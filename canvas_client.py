"""Canvas reads sent via the authenticated browser bridge only."""
from __future__ import annotations

import re
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

from config import Settings
from models import CanvasAssignment


class CanvasError(RuntimeError):
    pass


class BridgeRequester(Protocol):
    def request_canvas(self, url: str, method: str = "GET") -> dict[str, Any]: ...


def validate_canvas_url(settings: Settings, url: str, method: str = "GET") -> str:
    if method.upper() != "GET":
        raise CanvasError("Canvas bridge permits GET requests only; Canvas is never modified.")
    parsed = urlparse(url)
    origin = urlparse(settings.canvas_origin)
    if parsed.scheme != "https" or (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
        raise CanvasError("Rejected URL: it is not on the configured HTTPS Canvas origin.")
    if parsed.username or parsed.password or parsed.fragment:
        raise CanvasError("Rejected malformed Canvas URL.")
    return url


def _next_link(headers: dict[str, str]) -> str | None:
    link = headers.get("link") or headers.get("Link")
    if not link:
        return None
    for piece in link.split(","):
        if 'rel="next"' in piece or "rel=next" in piece:
            start, end = piece.find("<"), piece.find(">")
            if start >= 0 and end > start:
                return piece[start + 1:end]
    return None


class CanvasClient:
    def __init__(self, settings: Settings, bridge: BridgeRequester):
        self.settings, self.bridge = settings, bridge

    def _get(self, url: str) -> tuple[Any, dict[str, str]]:
        validate_canvas_url(self.settings, url)
        result = self.bridge.request_canvas(url)
        status = int(result.get("status", 0))
        if status == 599:
            detail = result.get("body", {}).get("error", "unknown Chrome extension error") if isinstance(result.get("body"), dict) else "unknown Chrome extension error"
            raise CanvasError(f"Chrome could not read Canvas: {detail}. Reload the Canvas tab and sign in again if needed.")
        if status in (401, 403):
            raise CanvasError("Canvas authentication has expired. Sign in to Canvas in Chrome, then retry.")
        if not 200 <= status < 300:
            raise CanvasError(f"Canvas returned HTTP {status} for a read-only GET request.")
        body = result.get("body")
        if not isinstance(body, (dict, list)):
            raise CanvasError("Canvas returned a non-JSON response; the Chrome session may be signed out.")
        return body, dict(result.get("headers") or {})

    def _paginate(self, first_url: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        url: str | None = first_url
        pages = 0
        while url:
            pages += 1
            if pages > self.settings.canvas_max_pages:
                raise CanvasError("Canvas pagination limit exceeded; increase canvas_max_pages only if expected.")
            data, headers = self._get(url)
            if not isinstance(data, list):
                raise CanvasError("Canvas list endpoint did not return an array.")
            output.extend(x for x in data if isinstance(x, dict))
            next_url = _next_link(headers)
            url = validate_canvas_url(self.settings, next_url) if next_url else None
        return output

    def discover_assignments(self) -> list[CanvasAssignment]:
        qs = urlencode({"enrollment_state": "active", "include[]": "term", "per_page": self.settings.canvas_page_limit})
        courses = self._paginate(f"{self.settings.canvas_origin}/api/v1/courses?{qs}")
        found: list[CanvasAssignment] = []
        unsupported: list[str] = []
        for course in courses:
            course_id = str(course.get("id", ""))
            code, name = course_identity(course, self.settings.course_code_overrides)
            if not course_id:
                continue
            if not code:
                unsupported.append(name or str(course.get("course_code") or course_id))
                continue
            params = urlencode({"per_page": self.settings.canvas_page_limit, "include[]": "submission"})
            rows = self._paginate(f"{self.settings.canvas_origin}/api/v1/courses/{course_id}/assignments?{params}")
            for row in rows:
                assignment_name = str(row.get("name") or "").strip()
                if not assignment_name:
                    continue
                assignment_id = str(row["id"]) if row.get("id") is not None else None
                html_url = row.get("html_url") if isinstance(row.get("html_url"), str) else None
                if html_url:
                    validate_canvas_url(self.settings, html_url)
                if not assignment_id and not html_url:
                    # Cannot idempotently identify this entry; omit it rather than risk duplicates.
                    continue
                found.append(CanvasAssignment(assignment_id, assignment_name, course_id, code, name, row.get("due_at"), html_url))
        if unsupported:
            raise CanvasError("Canvas courses without a usable Canvas/SIS course code cannot be synchronized: " + ", ".join(unsupported))
        return found


def course_identity(course: dict[str, Any], overrides: dict[str, str] | None = None) -> tuple[str, str]:
    """Return a Canvas-provided course code and human course title without inventing either.

    Some institutions overload ``course_code`` with a comma-delimited list of
    section identifiers. A Canvas ``sis_course_id`` is preferred. If it is not
    available, a trailing parenthesized identifier in the Canvas display name
    (for example ``Analytical Geometry I(MATH2450.B)``) is an explicit Canvas
    code and is safe to use; the parenthetical is excluded from the title.
    """
    display_name = str(course.get("name") or "").strip()
    course_code = str(course.get("course_code") or "").strip()
    sis_course_id = str(course.get("sis_course_id") or "").strip()
    parenthetical_code: str | None = None
    title = display_name
    for candidate in (display_name, course_code):
        match = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", candidate)
        if match and match.group(1).strip() and match.group(2).strip():
            if candidate == display_name:
                title = match.group(1).strip()
            parenthetical_code = match.group(2).strip()
            break
    override = (
        (overrides or {}).get(str(course.get("id", "")))
        or (overrides or {}).get(display_name)
        or (overrides or {}).get(title)
    )
    code = override or sis_course_id or parenthetical_code or (course_code if "," not in course_code else "")
    if "," in code:
        code = ""
    return code, title or display_name or course_code
