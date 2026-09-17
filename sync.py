from __future__ import annotations

import argparse
import os
import sys
import time

from bridge import BridgeError, BridgeRequester
from canvas_client import CanvasClient, CanvasError
from config import ConfigError, get_or_create_bridge_secret, load_dotenv, load_settings
from notion_client import NotionClient, NotionError
from sync_service import run_sync


def print_report(summary: dict[str, object], settings, notion_ready: bool, canvas_ready: bool, bridge_status: dict | None = None) -> None:
    print("Validation report")
    print(f"Database reachable: {'yes' if notion_ready else 'no'}")
    print(f"Integration authorized: {'yes' if notion_ready else 'no'}")
    print("Python dependencies: yes")
    print(f"Local bridge available: {'yes' if bridge_status and bridge_status.get('ok') else 'no'}")
    print(f"Chrome extension available: {'yes' if bridge_status and bridge_status.get('extension_connected') else 'no'}")
    print(f"Canvas connection available: {'yes' if canvas_ready else 'no'}")
    schema = summary.get("schema")
    if schema:
        expected = {"assignment": {"title", "rich_text"}, "course": {"select"}, "due_date": {"date"}, "time": {"select"}, "status": {"select", "status"}, "submission": {"select"}, "link": {"url"}}
        for key, types in expected.items():
            name = settings.property_names[key]
            prop = schema.properties.get(name)
            print(f"Property {name} found: {'yes' if prop else 'no'}; type correct: {'yes' if prop and prop.get('type') in types else 'no'}")
        for key, value in (("Time", settings.time_option), ("Status", settings.default_status), ("Submission", settings.submission_option)):
            prop = schema.properties.get(settings.property_names[key.lower()])
            kind = prop.get("type") if prop else ""
            options = {x.get("name") for x in prop.get(kind, {}).get("options", [])} if prop else set()
            print(f"Required {key} option '{value}' found: {'yes' if value in options else 'no'}")
        for value in summary.get("course_values", []):
            prop = schema.properties.get(settings.property_names["course"])
            options = {x.get("name") for x in prop.get("select", {}).get("options", [])} if prop else set()
            print(f"Required Course option '{value}' found: {'yes' if value in options else 'no'}")
    for key in ("assignments_discovered", "pages_created", "duplicates_skipped", "existing_pages_preserved", "failures"):
        print(f"{key.replace('_', ' ').capitalize()}: {summary.get(key, 0)}")
    if summary.get("missing_due_dates"): print("Assignments with no due date: " + ", ".join(summary["missing_due_dates"]))
    for error in summary.get("validation_errors", []): print("ERROR: " + error)
    for name in summary.get("ambiguous_matches", []): print("ERROR: ambiguous Notion match for " + name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Canvas assignment sync to Notion")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--sync", action="store_true")
    group.add_argument("--bridge-secret", action="store_true")
    args = parser.parse_args()
    if args.bridge_secret:
        print(get_or_create_bridge_secret()); return 0
    try:
        load_dotenv(); settings = load_settings()
        token = os.environ.get("NOTION_TOKEN", "")
        notion = NotionClient(settings, token)
        requester = BridgeRequester(settings)
        print("Checking local bridge…", flush=True)
        bridge_status = requester.health()
        if not bridge_status.get("extension_connected"):
            print("Waiting for the Chrome extension to connect (up to 35 seconds)…", flush=True)
            for _ in range(7):
                time.sleep(5)
                bridge_status = requester.health()
                if bridge_status.get("extension_connected"):
                    break
            else:
                raise BridgeError(
                    "Chrome extension is not polling the local bridge. In chrome://extensions, "
                    "reload ‘Canvas to Notion Local Bridge’, open its Options, confirm the Canvas "
                    "origin and bridge secret, then save/grant Canvas access. Keep Chrome running."
                )
        print("Chrome extension connected. Reading Canvas assignments…", flush=True)
        canvas = CanvasClient(settings, requester)
        # validate intentionally reads both systems; dry run makes no writes.
        summary = run_sync(canvas, notion, dry_run=args.dry_run or args.validate)
        print_report(summary, settings, True, True, requester.health())
        if args.dry_run:
            print("Dry run only: no Notion pages were created or updated.")
        return 1 if summary["validation_errors"] or summary["failures"] else 0
    except (ConfigError, NotionError, CanvasError, BridgeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2

if __name__ == "__main__": sys.exit(main())
