from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from canvas_client import CanvasClient
from models import CanvasAssignment, PreparedAssignment
from notion_client import NotionClient


def normalize_course_name(value: str) -> str:
    """Trim, remove all non-alphanumerics, and retain Unicode letters/digits in order."""
    return "".join(char for char in value.strip() if char.isalnum())


def parse_pacific(value: str | None, timezone: str) -> datetime | None:
    if not value: return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None: raise ValueError("Canvas due date lacks a timezone offset; refusing naive timestamp.")
    return parsed.astimezone(ZoneInfo(timezone))


def prepare(items: list[CanvasAssignment], timezone: str) -> tuple[list[PreparedAssignment], list[str]]:
    output, no_due = [], []
    for item in items:
        normalized = normalize_course_name(item.course_name)
        if not normalized: raise ValueError(f"Course '{item.course_code}' has no usable course name.")
        course_value = f"{item.course_code.strip()}/{normalized}"
        if "," in course_value:
            raise ValueError(f"Course '{item.course_code}' cannot be represented as a Notion Select because Notion option names cannot contain commas.")
        output.append(PreparedAssignment(item, course_value, parse_pacific(item.due_at, timezone)))
        if not item.due_at: no_due.append(item.name)
    return output, no_due


def _equal(page_fields: tuple[str, str, str | None, str | None, str | None], assignment: PreparedAssignment) -> bool:
    name, course, due, link, duplicate = page_fields
    desired_due = assignment.due_date.isoformat() if assignment.due_date else None
    identity_ok = duplicate in (None, assignment.source.duplicate_key)
    return (name, course, due, link) == (assignment.source.name, assignment.course_value, desired_due, assignment.source.html_url) and identity_ok


@dataclass(frozen=True)
class PlannedChange:
    kind: str
    assignment: PreparedAssignment
    page_id: str | None = None


def _summary(assignments: list[PreparedAssignment], no_due: list[str], schema: object, errors: list[str]) -> dict[str, object]:
    return {"assignments_discovered": len(assignments), "pages_created": 0, "duplicates_skipped": 0, "existing_pages_preserved": 0, "failures": 0, "missing_due_dates": no_due, "course_values": sorted({x.course_value for x in assignments}), "would_create": [], "would_update": [], "ambiguous_matches": [], "validation_errors": errors, "schema": schema}


def discover_and_validate(canvas: CanvasClient, notion: NotionClient) -> tuple[list[PreparedAssignment], dict[str, object]]:
    """Read Canvas once, then validate the Notion schema against that one snapshot."""
    assignments, no_due = prepare(canvas.discover_assignments(), notion.settings.pacific_timezone)
    schema, errors = notion.validate_schema(assignments)
    return assignments, _summary(assignments, no_due, schema, errors)


def build_plan(notion: NotionClient, assignments: list[PreparedAssignment], summary: dict[str, object]) -> list[PlannedChange]:
    """Query Notion once and make an immutable create/update plan without writes."""
    if summary["validation_errors"]:
        return []
    pages = notion.query_pages()
    by_link: dict[str, list[dict]] = {}
    by_duplicate_key: dict[str, list[dict]] = {}
    by_fallback: dict[tuple[str, str, str | None], list[dict]] = {}
    for page in pages:
        name, course, due, link, duplicate = notion.page_match_fields(page)
        if link: by_link.setdefault(link, []).append(page)
        if duplicate: by_duplicate_key.setdefault(duplicate, []).append(page)
        by_fallback.setdefault((name, course, due), []).append(page)
    changes: list[PlannedChange] = []
    for item in assignments:
        key = item.source.html_url
        if notion.settings.duplicate_key_property:
            matches = by_duplicate_key.get(item.source.duplicate_key, [])
        else:
            matches = by_link.get(key, []) if key else by_fallback.get((item.source.name, item.course_value, item.due_date.isoformat() if item.due_date else None), [])
        if len(matches) > 1:
            summary["ambiguous_matches"].append(item.source.name); summary["failures"] += 1; continue
        if not matches:
            summary["would_create"].append(item.source.name)
            changes.append(PlannedChange("create", item))
            continue
        page = matches[0]
        if _equal(notion.page_match_fields(page), item):
            summary["duplicates_skipped"] += 1; summary["existing_pages_preserved"] += 1
        else:
            summary["would_update"].append(item.source.name)
            changes.append(PlannedChange("update", item, page["id"]))
            summary["existing_pages_preserved"] += 1
    return changes


def apply_plan(notion: NotionClient, changes: list[PlannedChange], summary: dict[str, object]) -> dict[str, object]:
    """Perform the already-reviewed plan; Status is still omitted on updates."""
    if summary["validation_errors"] or summary["failures"]:
        return summary
    schema = summary["schema"]
    for change in changes:
        if change.kind == "create":
            notion.create_page(change.assignment, schema)
            summary["pages_created"] += 1
        else:
            notion.update_page(change.page_id or "", change.assignment, schema)
    summary["would_create"] = []
    summary["would_update"] = []
    return summary


def run_sync(canvas: CanvasClient, notion: NotionClient, dry_run: bool = False) -> dict[str, object]:
    """Existing single-mode entry point retained for validate/dry-run/sync commands."""
    assignments, summary = discover_and_validate(canvas, notion)
    changes = build_plan(notion, assignments, summary)
    return summary if dry_run else apply_plan(notion, changes, summary)


def run_all(canvas: CanvasClient, notion: NotionClient) -> tuple[dict[str, object], dict[str, object] | None, dict[str, object] | None]:
    """One-pass workflow: Canvas read once → validate → plan → apply the same plan."""
    assignments, validation = discover_and_validate(canvas, notion)
    if validation["validation_errors"]:
        return validation, None, None
    plan = _summary(assignments, validation["missing_due_dates"], validation["schema"], [])
    changes = build_plan(notion, assignments, plan)
    if plan["failures"]:
        return validation, plan, None
    completed = dict(plan)
    completed["would_create"] = list(plan["would_create"])
    completed["would_update"] = list(plan["would_update"])
    completed = apply_plan(notion, changes, completed)
    return validation, plan, completed
