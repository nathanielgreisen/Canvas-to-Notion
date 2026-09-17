from __future__ import annotations

import io
import time
from urllib.error import HTTPError

import pytest

from bridge import BridgeError, JobQueue
from canvas_client import CanvasClient, CanvasError, course_identity, validate_canvas_url
from config import Settings
from models import CanvasAssignment, PreparedAssignment
from notion_client import NotionClient, NotionHTTPError, SchemaReport
from sync_service import normalize_course_name, parse_pacific, prepare, run_sync


def settings(**overrides):
    values = dict(canvas_origin="https://canvas.example.edu", bridge_port=8765, notion_database_id="database-id", notion_version="2026-03-11", property_names={"assignment":"Assignment","course":"Course","due_date":"Due Date","time":"Time","status":"Status","submission":"Submission","link":"Link"}, default_status="Not Started", time_option="30 minutes", submission_option="Canvas", duplicate_key_property=None, course_code_overrides={}, pacific_timezone="America/Los_Angeles", allow_create_select_options=False, bridge_job_ttl_seconds=1, bridge_response_max_bytes=10000, canvas_page_limit=100, canvas_max_pages=5)
    values.update(overrides); return Settings(**values)


@pytest.mark.parametrize("url", ["http://canvas.example.edu/api/v1/courses", "https://evil.example/api", "https://canvas.example.edu.evil/api"])
def test_rejects_non_canvas_urls(url):
    with pytest.raises(CanvasError): validate_canvas_url(settings(), url)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_rejects_canvas_write_methods(method):
    with pytest.raises(CanvasError): validate_canvas_url(settings(), "https://canvas.example.edu/api/v1/courses", method)


def test_bridge_authentication_and_expiry(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "SECRET_FILE", tmp_path / "secret")
    queue = JobQueue(settings(bridge_job_ttl_seconds=0))
    assert queue.authenticate(queue.secret) and not queue.authenticate("wrong")
    job = queue.submit("https://canvas.example.edu/api/v1/courses")
    time.sleep(0.01)
    assert queue.next_job(timeout=0.01) is None
    with pytest.raises(BridgeError): queue.complete(job.id, {"status": 200, "body": []})


class FakeBridge:
    def __init__(self): self.calls = []
    def request_canvas(self, url, method="GET"):
        self.calls.append(url)
        if "courses?" in url:
            return {"status":200,"body":[{"id":1,"course_code":"C 1","name":"My Course"}],"headers":{}}
        if "page=2" in url: return {"status":200,"body":[{"id":12,"name":"B","due_at":None,"html_url":"https://canvas.example.edu/courses/1/assignments/12"}],"headers":{}}
        return {"status":200,"body":[{"id":11,"name":"A","due_at":"2026-07-01T00:00:00Z","html_url":"https://canvas.example.edu/courses/1/assignments/11"}],"headers":{"Link":"<https://canvas.example.edu/api/v1/courses/1/assignments?page=2>; rel=\"next\""}}


def test_canvas_pagination_and_missing_due_dates():
    items = CanvasClient(settings(), FakeBridge()).discover_assignments()
    prepared, no_due = prepare(items, "America/Los_Angeles")
    assert [x.source.name for x in prepared] == ["A", "B"]
    assert no_due == ["B"]


def test_course_normalization_and_pacific_offsets():
    assert normalize_course_name(" The Christian-Faith! ") == "TheChristianFaith"
    assert parse_pacific("2026-01-15T20:00:00Z", "America/Los_Angeles").isoformat().endswith("-08:00")
    assert parse_pacific("2026-07-15T20:00:00Z", "America/Los_Angeles").isoformat().endswith("-07:00")
    with pytest.raises(ValueError): parse_pacific("2026-07-15T20:00:00", "America/Los_Angeles")


def test_course_identity_prefers_sis_id_and_reads_human_title():
    code, title = course_identity({"sis_course_id": "202710.TS.MATH2450.B", "course_code": "12345.202710.EA, 12391.202710.EA", "name": "Analytical Geometry and Calculus I(MATH2450.B)"})
    assert code == "202710.TS.MATH2450.B"
    assert title == "Analytical Geometry and Calculus I"
    code, title = course_identity({"course_code": "12345.202710.EA, 12391.202710.EA", "name": "Analytical Geometry and Calculus I(MATH2450.B)"})
    assert code == "MATH2450.B"
    assert title == "Analytical Geometry and Calculus I"
    code, title = course_identity({"id": 9, "course_code": "1, 2, 3", "name": "Extended Add Magnolia Singers (MUSC-0910-A, MUSC-4900C-A, MUSC-5610-A)"}, {"Extended Add Magnolia Singers": "MUSC0910"})
    assert code == "MUSC0910" and title == "Extended Add Magnolia Singers"


def schema():
    return SchemaReport("db", "ds", {"Assignment":{"type":"title"},"Course":{"type":"select","select":{"options":[{"name":"C/Name"}]}},"Due Date":{"type":"date"},"Time":{"type":"select","select":{"options":[{"name":"30 minutes"}]}},"Status":{"type":"select","select":{"options":[{"name":"Not Started"}]}},"Submission":{"type":"select","select":{"options":[{"name":"Canvas"}]}},"Link":{"type":"url"}})


def prepared():
    return PreparedAssignment(CanvasAssignment("5", "Essay", "1", "C", "Name", "2026-07-15T20:00:00Z", "https://canvas.example.edu/courses/1/assignments/5"), "C/Name", parse_pacific("2026-07-15T20:00:00Z", "America/Los_Angeles"))


def test_notion_payload_and_status_preservation():
    client = NotionClient(settings(), "secret_test")
    create = client.payload_for(prepared(), True, schema())
    update = client.payload_for(prepared(), False, schema())
    assert create["Assignment"]["title"][0]["text"]["content"] == "Essay"
    assert create["Due Date"]["date"]["start"].endswith("-07:00")
    assert create["Status"]["select"]["name"] == "Not Started"
    assert "Status" not in update
    assert update["Link"]["url"].endswith("/5")


class SchemaClient(NotionClient):
    def __init__(self, response): super().__init__(settings(), "secret_test"); self.response = response
    def inspect_schema(self): return self.response


def test_missing_properties_types_and_options_reported():
    incomplete = SchemaReport("db", "ds", {"Assignment":{"type":"url"}})
    _, errors = SchemaClient(incomplete).validate_schema([prepared()])
    assert any("Property 'Assignment'" in x for x in errors) and any("Missing property 'Course'" in x for x in errors)
    _, errors = SchemaClient(schema()).validate_schema([prepared()])
    assert errors == []
    bad = schema(); props = dict(bad.properties); props["Time"] = {"type":"select", "select":{"options":[]}}
    _, errors = SchemaClient(SchemaReport("db", "ds", props)).validate_schema([prepared()])
    assert any("Missing time select option" in x for x in errors)


def test_native_notion_status_property_is_supported():
    native = schema(); props = dict(native.properties)
    props["Status"] = {"type": "status", "status": {"options": [{"name": "Not Started"}]}}
    client = SchemaClient(SchemaReport("db", "ds", props))
    _, errors = client.validate_schema([prepared()])
    assert errors == []
    payload = client.payload_for(prepared(), True, SchemaReport("db", "ds", props))
    assert payload["Status"]["status"]["name"] == "Not Started"


class SyncCanvas:
    def discover_assignments(self): return [prepared().source]

class SyncNotion:
    settings = settings()
    def __init__(self, fields): self.fields, self.created, self.updated = fields, 0, 0
    def validate_schema(self, discovered): return schema(), []
    def query_pages(self): return [{"id":"p1", "properties":{}}]
    def page_match_fields(self, page): return self.fields
    def create_page(self, assignment, schema): self.created += 1
    def update_page(self, page_id, assignment, schema): self.updated += 1


def test_duplicate_detection_and_existing_status_preservation():
    item = prepared(); fields = (item.source.name, item.course_value, item.due_date.isoformat(), item.source.html_url, None)
    notion = SyncNotion(fields)
    summary = run_sync(SyncCanvas(), notion)
    assert summary["duplicates_skipped"] == 1 and notion.created == 0 and notion.updated == 0
    changed = SyncNotion(("Old", item.course_value, item.due_date.isoformat(), item.source.html_url, None))
    run_sync(SyncCanvas(), changed)
    assert changed.updated == 1  # update_page deliberately omits Status


def test_dry_run_never_writes_notion():
    notion = SyncNotion(("different", "different", None, None, None))
    summary = run_sync(SyncCanvas(), notion, dry_run=True)
    assert summary["would_create"] == ["Essay"]
    assert notion.created == 0 and notion.updated == 0


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 500, 503])
def test_notion_http_errors_are_clear(status, monkeypatch):
    def opener(_request, timeout=20):
        raise HTTPError("https://api.notion.com", status, "error", {}, io.BytesIO(b'{"message":"mock failure"}'))
    monkeypatch.setattr("notion_client.time.sleep", lambda _: None)
    with pytest.raises(NotionHTTPError) as exc: NotionClient(settings(), "secret_test", opener=opener)._request("GET", "/databases/x")
    assert exc.value.status == status


def test_validate_command_with_mocked_credentials(monkeypatch):
    import sync
    monkeypatch.setattr(sync, "load_dotenv", lambda: None)
    monkeypatch.setattr(sync, "load_settings", lambda: settings())
    monkeypatch.setenv("NOTION_TOKEN", "secret_mock")
    monkeypatch.setattr(sync, "NotionClient", lambda *_: object())
    class MockRequester:
        def health(self): return {"ok": True, "extension_connected": True}
    monkeypatch.setattr(sync, "BridgeRequester", lambda *_: MockRequester())
    monkeypatch.setattr(sync, "CanvasClient", lambda *_: object())
    monkeypatch.setattr(sync, "run_sync", lambda *_args, **_kwargs: {"assignments_discovered": 1, "pages_created": 0, "duplicates_skipped": 1, "existing_pages_preserved": 1, "failures": 0, "missing_due_dates": [], "validation_errors": [], "ambiguous_matches": []})
    monkeypatch.setattr("sys.argv", ["sync.py", "--validate"])
    assert sync.main() == 0
