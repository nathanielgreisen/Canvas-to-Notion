"""Small official Notion REST API client; tokens remain in this process only."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import Settings
from models import PreparedAssignment


class NotionError(RuntimeError):
    pass


class NotionHTTPError(NotionError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        remedies = {401: "Check NOTION_TOKEN.", 403: "Give the integration edit access to the database.", 404: "Check the database ID and share the database with the integration.", 409: "Notion reported a conflict; retry the sync.", 429: "Notion rate-limited the request; it will be retried.", 500: "Notion had a server error; retry later."}
        super().__init__(f"Notion HTTP {status}: {detail}. {remedies.get(status, '')}".strip())


@dataclass(frozen=True)
class SchemaReport:
    database_id: str
    data_source_id: str
    properties: dict[str, Any]


def _title_text(prop: dict[str, Any]) -> str:
    chunks = prop.get("title", []) if prop.get("type") == "title" else prop.get("rich_text", [])
    return "".join(x.get("plain_text", "") for x in chunks if isinstance(x, dict))


def _date_start(prop: dict[str, Any]) -> str | None:
    date = prop.get("date") or {}
    return date.get("start") if isinstance(date, dict) else None


class NotionClient:
    api = "https://api.notion.com/v1"
    def __init__(self, settings: Settings, token: str, opener=urlopen):
        if not token:
            raise NotionError("NOTION_TOKEN is missing. Add it to local .env; never put it in the extension.")
        self.settings, self.token, self.opener = settings, token, opener
        self.data_source_id: str | None = None

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.api + path, data=body, method=method, headers={"Authorization": f"Bearer {self.token}", "Notion-Version": self.settings.notion_version, "Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with self.opener(request, timeout=20) as response:
                    return json.loads(response.read())
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")[:500]
                try: detail = json.loads(raw).get("message", raw)
                except json.JSONDecodeError: detail = raw
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < 4:
                        retry_after = exc.headers.get("Retry-After") if exc.headers else None
                        time.sleep(float(retry_after) if retry_after else min(8, 0.5 * (2 ** attempt)))
                        continue
                raise NotionHTTPError(exc.code, detail) from exc
            except URLError as exc:
                if attempt < 4:
                    time.sleep(min(8, 0.5 * (2 ** attempt))); continue
                raise NotionError("Cannot reach Notion. Check network access and retry.") from exc
        raise AssertionError("unreachable")

    def inspect_schema(self) -> SchemaReport:
        database = self._request("GET", f"/databases/{self.settings.notion_database_id}")
        sources = database.get("data_sources", [])
        if not sources:
            # Some legacy database responses have properties directly; retain compatibility.
            if database.get("properties"):
                self.data_source_id = self.settings.notion_database_id
                return SchemaReport(self.settings.notion_database_id, self.data_source_id, database["properties"])
            raise NotionError("Database has no accessible data source. Use a table database shared with the integration.")
        if len(sources) != 1:
            raise NotionError("Database has multiple data sources. Configure a single data-source database for this version.")
        self.data_source_id = sources[0]["id"]
        data_source = self._request("GET", f"/data_sources/{self.data_source_id}")
        return SchemaReport(self.settings.notion_database_id, self.data_source_id, data_source["properties"])

    def validate_schema(self, discovered: Iterable[PreparedAssignment] = ()) -> tuple[SchemaReport, list[str]]:
        report = self.inspect_schema()
        errors: list[str] = []
        expected = {"assignment": {"title", "rich_text"}, "course": {"select"}, "due_date": {"date"}, "time": {"select"}, "status": {"select", "status"}, "submission": {"select"}, "link": {"url"}}
        for key, allowed in expected.items():
            name = self.settings.property_names[key]
            prop = report.properties.get(name)
            if not prop:
                errors.append(f"Missing property '{name}'. Create it as {sorted(allowed)[0]}.")
            elif prop.get("type") not in allowed:
                errors.append(f"Property '{name}' must be {', '.join(sorted(allowed))}, but is {prop.get('type')!r}.")
        if self.settings.duplicate_key_property:
            prop = report.properties.get(self.settings.duplicate_key_property)
            if not prop:
                errors.append(f"Missing approved duplicate-key property '{self.settings.duplicate_key_property}'. Create it as rich_text.")
            elif prop.get("type") != "rich_text":
                errors.append(f"Approved duplicate-key property '{self.settings.duplicate_key_property}' must be rich_text, but is {prop.get('type')!r}.")
        if errors:
            return report, errors
        required_options = [("time", self.settings.time_option), ("status", self.settings.default_status), ("submission", self.settings.submission_option)]
        course_values = sorted({x.course_value for x in discovered})
        required_options.extend(("course", value) for value in course_values)
        missing: list[tuple[str, str]] = []
        for key, option in required_options:
            prop = report.properties[self.settings.property_names[key]]
            kind = prop["type"]
            options = prop.get(kind, {}).get("options", [])
            if option not in {x.get("name") for x in options}:
                missing.append((key, option))
        if missing and self.settings.allow_create_select_options:
            self._add_select_options(report, missing)
            report = self.inspect_schema()
        elif missing:
            errors.extend(f"Missing {key} select option '{value}'. Add it in Notion, or set allow_create_select_options to true." for key, value in missing)
        return report, errors

    def _add_select_options(self, report: SchemaReport, missing: list[tuple[str, str]]) -> None:
        by_key: dict[str, set[str]] = {}
        for key, value in missing:
            # Status has special semantics and is deliberately never created automatically.
            if key == "status":
                raise NotionError(f"Missing Status option '{value}'. Add it manually in Notion before syncing.")
            by_key.setdefault(key, set()).add(value)
        updates: dict[str, Any] = {}
        for key, values in by_key.items():
            name = self.settings.property_names[key]
            prop = report.properties[name]
            old = prop["select"].get("options", [])
            updates[name] = {"select": {"options": [{"id": x["id"], "name": x["name"], "color": x.get("color", "default")} for x in old] + [{"name": v, "color": "default"} for v in sorted(values)]}}
        if updates:
            self._request("PATCH", f"/data_sources/{report.data_source_id}", {"properties": updates})

    def query_pages(self) -> list[dict[str, Any]]:
        if not self.data_source_id:
            raise NotionError("Schema must be inspected before querying pages.")
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100, "result_type": "page"}
            if cursor: payload["start_cursor"] = cursor
            response = self._request("POST", f"/data_sources/{self.data_source_id}/query", payload)
            result.extend(response.get("results", []))
            if not response.get("has_more"): return result
            cursor = response.get("next_cursor")
            if not cursor: raise NotionError("Notion pagination response was incomplete.")

    def payload_for(self, assignment: PreparedAssignment, include_status: bool, schema: SchemaReport) -> dict[str, Any]:
        p = self.settings.property_names
        title_kind = schema.properties[p["assignment"]]["type"]
        text_value = {"type": "text", "text": {"content": assignment.source.name}}
        payload: dict[str, Any] = {
            p["assignment"]: {title_kind: [text_value]},
            p["course"]: {"select": {"name": assignment.course_value}},
            p["due_date"]: {"date": {"start": assignment.due_date.isoformat()} if assignment.due_date else None},
            p["time"]: {"select": {"name": self.settings.time_option}},
            p["submission"]: {"select": {"name": self.settings.submission_option}},
        }
        if assignment.source.html_url:
            payload[p["link"]] = {"url": assignment.source.html_url}
        else:
            payload[p["link"]] = {"url": None}
        if include_status:
            kind = schema.properties[p["status"]]["type"]
            payload[p["status"]] = {kind: {"name": self.settings.default_status}}
        if self.settings.duplicate_key_property:
            payload[self.settings.duplicate_key_property] = {"rich_text": [{"type": "text", "text": {"content": assignment.source.duplicate_key}}]}
        return payload

    def create_page(self, assignment: PreparedAssignment, schema: SchemaReport) -> dict[str, Any]:
        return self._request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": schema.data_source_id}, "properties": self.payload_for(assignment, True, schema)})

    def update_page(self, page_id: str, assignment: PreparedAssignment, schema: SchemaReport) -> dict[str, Any]:
        # Status intentionally omitted; this preserves it exactly.
        return self._request("PATCH", f"/pages/{page_id}", {"properties": self.payload_for(assignment, False, schema)})

    def page_match_fields(self, page: dict[str, Any]) -> tuple[str, str, str | None, str | None, str | None]:
        p = self.settings.property_names; props = page.get("properties", {})
        duplicate = _title_text(props.get(self.settings.duplicate_key_property, {})) if self.settings.duplicate_key_property else None
        return (_title_text(props.get(p["assignment"], {})), (props.get(p["course"], {}).get("select") or {}).get("name") or "", _date_start(props.get(p["due_date"], {})), props.get(p["link"], {}).get("url"), duplicate)
