"""Authenticated localhost job queue. It never receives Canvas cookies."""
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from canvas_client import CanvasError, validate_canvas_url
from config import Settings, get_or_create_bridge_secret


class BridgeError(RuntimeError):
    pass


@dataclass
class Job:
    id: str
    url: str
    created_at: float
    expires_at: float
    result: dict[str, Any] | None = None
    error: str | None = None
    event: threading.Event = field(default_factory=threading.Event)


class JobQueue:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secret = get_or_create_bridge_secret()
        self.jobs: dict[str, Job] = {}
        self.pending: queue.Queue[str] = queue.Queue()
        self.extension_seen_at: float | None = None
        self.lock = threading.Lock()

    def authenticate(self, supplied: str | None) -> bool:
        return bool(supplied) and secrets.compare_digest(supplied, self.secret)

    def submit(self, url: str) -> Job:
        validate_canvas_url(self.settings, url)
        now = time.monotonic()
        job = Job(secrets.token_urlsafe(18), url, now, now + self.settings.bridge_job_ttl_seconds)
        with self.lock:
            self._cleanup(now)
            self.jobs[job.id] = job
            self.pending.put(job.id)
        return job

    def next_job(self, timeout: float = 25) -> Job | None:
        self.extension_seen_at = time.monotonic()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                job_id = self.pending.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return None
            with self.lock:
                job = self.jobs.get(job_id)
                if job and time.monotonic() < job.expires_at and not job.result and not job.error:
                    return job
                if job:
                    job.error = "Canvas bridge job expired before Chrome collected it."
                    job.event.set()
        return None

    def complete(self, job_id: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        if len(encoded) > self.settings.bridge_response_max_bytes:
            raise BridgeError("Canvas response exceeds configured response-size limit.")
        if not isinstance(payload.get("status"), int) or not isinstance(payload.get("body"), (dict, list)):
            raise BridgeError("Malformed Canvas bridge result.")
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or time.monotonic() >= job.expires_at:
                raise BridgeError("Job is unknown or expired; do not retry its Canvas request.")
            job.result = payload
            job.event.set()

    def wait(self, job: Job, timeout: float = 100) -> dict[str, Any]:
        if not job.event.wait(timeout):
            if self.extension_seen_at is None or time.monotonic() - self.extension_seen_at > 40:
                raise BridgeError("Chrome extension is disconnected. Load it, configure it, and keep Chrome running.")
            raise BridgeError("Canvas did not answer before the bridge timeout. Reload the Canvas tab and retry.")
        if job.error:
            raise BridgeError(job.error)
        if not job.result:
            raise BridgeError("Canvas bridge returned no result.")
        return job.result

    def _cleanup(self, now: float) -> None:
        for key, job in list(self.jobs.items()):
            if now > job.expires_at + 300:
                del self.jobs[key]


class BridgeRequester:
    """Client used by sync.py; it submits only validated GET jobs to localhost."""
    def __init__(self, settings: Settings):
        self.settings, self.secret = settings, get_or_create_bridge_secret()

    def request_canvas(self, url: str, method: str = "GET") -> dict[str, Any]:
        validate_canvas_url(self.settings, url, method)
        data = json.dumps({"url": url, "method": "GET"}).encode()
        request = Request(self.settings.bridge_url + "/v1/jobs", data=data, headers={"Content-Type": "application/json", "X-Bridge-Secret": self.secret}, method="POST")
        try:
            with urlopen(request, timeout=8) as response:
                started = json.loads(response.read())
            job_request = Request(self.settings.bridge_url + f"/v1/jobs/{started['job_id']}/wait", headers={"X-Bridge-Secret": self.secret})
            with urlopen(job_request, timeout=self.settings.bridge_job_ttl_seconds + 15) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            raise BridgeError(f"Local bridge rejected the request (HTTP {exc.code}). Start server.py and check its configuration.") from exc
        except URLError as exc:
            raise BridgeError("Local bridge is unavailable. Start `python3 server.py` in this project.") from exc

    def health(self) -> dict[str, Any]:
        request = Request(self.settings.bridge_url + "/v1/health", headers={"X-Bridge-Secret": self.secret})
        try:
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read())
        except (HTTPError, URLError) as exc:
            raise BridgeError("Local bridge is unavailable or rejected its secret. Start server.py and reconfigure the extension secret if needed.") from exc


def make_handler(queue_: JobQueue):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CanvasNotionBridge/1"
        def log_message(self, format: str, *args: object) -> None:  # avoid sensitive bodies
            return
        def _send(self, code: int, body: dict[str, Any] | None = None) -> None:
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Bridge-Secret"); self.end_headers()
            if body is not None: self.wfile.write(json.dumps(body).encode())
        def _auth(self) -> bool:
            if not queue_.authenticate(self.headers.get("X-Bridge-Secret")):
                self._send(401, {"error": "invalid bridge authentication"}); return False
            return True
        def do_OPTIONS(self) -> None: self._send(204)
        def do_GET(self) -> None:
            if not self._auth(): return
            if self.path == "/v1/health":
                self._send(200, {"ok": True, "extension_connected": queue_.extension_seen_at is not None and time.monotonic() - queue_.extension_seen_at < 40}); return
            if self.path == "/v1/jobs/next":
                job = queue_.next_job()
                self._send(200, {"job_id": job.id, "url": job.url, "method": "GET"} if job else {"job": None}); return
            if self.path.startswith("/v1/jobs/") and self.path.endswith("/wait"):
                job_id = self.path.split("/")[3]
                with queue_.lock: job = queue_.jobs.get(job_id)
                if not job: self._send(404, {"error": "unknown job"}); return
                try: self._send(200, queue_.wait(job, queue_.settings.bridge_job_ttl_seconds + 10))
                except BridgeError as exc: self._send(504, {"error": str(exc)})
                return
            self._send(404, {"error": "not found"})
        def do_POST(self) -> None:
            if not self._auth(): return
            length = int(self.headers.get("Content-Length", "0"))
            if length > queue_.settings.bridge_response_max_bytes: self._send(413, {"error": "request too large"}); return
            try: payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError): self._send(400, {"error": "invalid JSON"}); return
            try:
                if self.path == "/v1/jobs":
                    if payload.get("method") != "GET": raise CanvasError("Only Canvas GET jobs are permitted.")
                    job = queue_.submit(str(payload.get("url", ""))); self._send(202, {"job_id": job.id}); return
                if self.path.startswith("/v1/jobs/") and self.path.endswith("/result"):
                    queue_.complete(self.path.split("/")[3], payload); self._send(200, {"ok": True}); return
                self._send(404, {"error": "not found"})
            except (BridgeError, CanvasError) as exc: self._send(400, {"error": str(exc)})
    return Handler


def run_server(settings: Settings) -> None:
    queue_ = JobQueue(settings)
    httpd = ThreadingHTTPServer(("127.0.0.1", settings.bridge_port), make_handler(queue_))
    print(f"Canvas–Notion bridge listening on 127.0.0.1:{settings.bridge_port}. Secret: {get_or_create_bridge_secret()}")
    print("Copy the secret into the extension Options page once. Do not share it.")
    httpd.serve_forever()
