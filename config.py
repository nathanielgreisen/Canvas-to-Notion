"""Configuration and secure local-state helpers."""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


STATE_DIR = Path.home() / ".canvas-notion-bridge"
SECRET_FILE = STATE_DIR / "bridge_secret"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    canvas_origin: str
    bridge_port: int
    notion_database_id: str
    notion_version: str
    property_names: dict[str, str]
    default_status: str
    time_option: str
    submission_option: str
    duplicate_key_property: str | None
    course_code_overrides: dict[str, str]
    pacific_timezone: str
    allow_create_select_options: bool
    bridge_job_ttl_seconds: int
    bridge_response_max_bytes: int
    canvas_page_limit: int
    canvas_max_pages: int

    @property
    def bridge_url(self) -> str:
        return f"http://127.0.0.1:{self.bridge_port}"


def load_dotenv(path: Path = Path(".env")) -> None:
    """Small dependency-free .env reader; existing environment wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _expand(value: object) -> object:
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def validate_canvas_origin(origin: str) -> str:
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ConfigError("canvas_origin must be an HTTPS origin only, e.g. https://canvas.example.edu")
    return f"https://{parsed.netloc}".rstrip("/")


def load_settings(path: Path = Path("config.json")) -> Settings:
    load_dotenv()
    if not path.exists():
        raise ConfigError("config.json is missing. Copy config.example.json to config.json and edit it.")
    try:
        raw = _expand(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from exc
    try:
        props = raw["property_names"]
        required = {"assignment", "course", "due_date", "time", "status", "submission", "link"}
        if set(props) != required or not all(isinstance(v, str) and v.strip() for v in props.values()):
            raise ConfigError("property_names must contain exactly assignment, course, due_date, time, status, submission, link")
        database_id = str(raw.get("notion_database_id") or os.environ.get("NOTION_DATABASE_ID", ""))
        overrides_raw = raw.get("course_code_overrides", {})
        if not isinstance(overrides_raw, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip() and "," not in v for k, v in overrides_raw.items()):
            raise ConfigError("course_code_overrides must be an object of Canvas course IDs or exact titles to comma-free, user-approved codes.")
        select_values = (str(raw["default_status"]), str(raw["time_option"]), str(raw["submission_option"]))
        if any("," in value for value in select_values):
            raise ConfigError("Notion select option names cannot contain commas; adjust default_status, time_option, or submission_option.")
        return Settings(
            canvas_origin=validate_canvas_origin(str(raw["canvas_origin"])),
            bridge_port=int(raw.get("bridge_port", 8765)),
            notion_database_id=database_id,
            notion_version=str(raw.get("notion_version", "2026-03-11")),
            property_names=dict(props), default_status=str(raw["default_status"]),
            time_option=str(raw["time_option"]), submission_option=str(raw["submission_option"]),
            duplicate_key_property=(str(raw["duplicate_key_property"]).strip() if raw.get("duplicate_key_property") else None),
            course_code_overrides={key.strip(): value.strip() for key, value in overrides_raw.items()},
            pacific_timezone=str(raw.get("pacific_timezone", "America/Los_Angeles")),
            allow_create_select_options=bool(raw.get("allow_create_select_options", False)),
            bridge_job_ttl_seconds=int(raw.get("bridge_job_ttl_seconds", 90)),
            bridge_response_max_bytes=int(raw.get("bridge_response_max_bytes", 2_097_152)),
            canvas_page_limit=min(max(int(raw.get("canvas_page_limit", 100)), 1), 100),
            canvas_max_pages=min(max(int(raw.get("canvas_max_pages", 50)), 1), 100),
        )
    except KeyError as exc:
        raise ConfigError(f"config.json is missing required key: {exc.args[0]}") from exc


def get_or_create_bridge_secret() -> str:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(32)
    SECRET_FILE.write_text(secret + "\n", encoding="utf-8")
    SECRET_FILE.chmod(0o600)
    return secret
