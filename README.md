# Canvas to Notion — read-only local assignment sync

This macOS project copies homework assignments visible to the signed-in Chrome user into one Notion table. It only reads Canvas assignments; it does not submit work or read grades, messages, files, discussions, pages, or other course content.

The flow is `sync.py → localhost queue → Chrome extension → signed-in Canvas tab → Canvas GET API → localhost → Notion API`. Canvas cookies never leave Chrome. The extension has no cookie permission, stores no cookies, and only makes credentialed `GET` fetches from a Canvas content script. The Python server binds to `127.0.0.1` only.

## Setup on macOS

From this project directory:

```zsh
cp config.example.json config.json
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Edit `.env` locally (it is already ignored by Git) with these exact placeholder keys:

```dotenv
NOTION_TOKEN=secret_xxxxxxxxxxxxxxxxxxxxxxxxxx
NOTION_DATABASE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Do not put either value in source code, `config.json`, or the Chrome extension. `config.json` may refer to `${NOTION_DATABASE_ID}` as the supplied example does.

Create a Notion **internal integration** at [Notion integrations](https://www.notion.so/profile/integrations), enable read and update content capabilities, and copy its secret token into `NOTION_TOKEN`. Open the target database in Notion, use **••• → Add connections**, and select the integration; a 404 from validation almost always means that sharing step or the database ID is wrong. Copy the database ID from the table URL (the 32-character UUID portion, with or without hyphens).

Edit `config.json`. It is the one documented application configuration file:

```json
{
  "canvas_origin": "https://canvas.example.edu",
  "bridge_port": 8765,
  "notion_database_id": "${NOTION_DATABASE_ID}",
  "notion_version": "2026-03-11",
  "property_names": {
    "assignment": "Assignment",
    "course": "Course",
    "due_date": "Due Date",
    "time": "Time",
    "status": "Status",
    "submission": "Submission",
    "link": "Link"
  },
  "default_status": "Not Started",
  "time_option": "30 minutes",
  "submission_option": "Canvas",
  "duplicate_key_property": null,
  "course_code_overrides": {},
  "pacific_timezone": "America/Los_Angeles",
  "allow_create_select_options": false,
  "bridge_job_ttl_seconds": 90,
  "bridge_response_max_bytes": 2097152,
  "canvas_page_limit": 100,
  "canvas_max_pages": 50
}
```

`canvas_origin` must be an HTTPS origin only, for example `https://canvas.school.edu`, with no path. The extension’s Canvas origin must exactly match it. The integration uses current Notion data-source endpoints and `Notion-Version: 2026-03-11`; its property payloads follow the official [create-page](https://developers.notion.com/reference/post-page) and [property-value](https://developers.notion.com/reference/property-item-object) documentation.

## Required Notion table properties

Configure the names in `property_names` exactly as they appear in your Notion table. The defaults require:

| Property | Required type | Required option/value |
| --- | --- | --- |
| Assignment | Title or Text | Canvas assignment name |
| Course | Select | `CLASSCODE/NormalizedCourseName` |
| Due Date | Date | ISO-8601 Pacific date-time or empty |
| Time | Select | `30 minutes` |
| Status | Select or Status | `Not Started` for newly created pages only |
| Submission | Select | `Canvas` |
| Link | URL | Canvas-provided assignment URL or empty |

Before any write, validation verifies names, types, and select options. With `allow_create_select_options: false` (the default), add missing Time, Status, Submission, and Course options manually in Notion. Setting it to true may add missing **Course**, Time, and Submission select options; it never creates a missing Status option automatically. This protects your Status workflow. No other fields are created or modified.

Course formatting is deterministic: whitespace is trimmed, Canvas’s SIS course ID is preferred over `course_code`, and the course name has every non-alphanumeric character (including spaces and punctuation) removed while preserving Unicode letters/digits. Thus `The Christian-Faith!` becomes `TheChristianFaith`. Some Canvas sites put a comma-delimited section list in `course_code`; that is rejected rather than used. If no SIS ID is available, a code explicitly shown in the Canvas title’s trailing parentheses is used (for example `Analytical Geometry I(MATH2450.B)` → code `MATH2450.B`, title `Analytical Geometry I`). For an intentional cross-listing, add an explicit `course_code_overrides` entry keyed by the Canvas course ID (preferred) or exact Canvas course title; this is a user-approved code, not an inferred value. A course with no usable Canvas-provided or user-approved code stops the run.

## Install the Chrome extension

1. Open Chrome and visit `chrome://extensions`.
2. Turn on **Developer mode**.
3. Choose **Load unpacked**, then select this project’s `extension` directory.
4. Sign in to the same Canvas site in Chrome normally.
5. Start the local server below once, then open the extension’s **Details → Extension options**.
6. Set Canvas origin to the exact `canvas_origin` from `config.json`; leave bridge URL as `http://127.0.0.1:8765` unless you changed the port.
7. Obtain the local bridge secret with `.venv/bin/python sync.py --bridge-secret`, paste it into **Bridge secret**, and choose **Save and grant Canvas access**. This asks Chrome for that exact HTTPS Canvas host only.

The bridge secret is generated on first use at `~/.canvas-notion-bridge/bridge_secret` with owner-only permissions. It authenticates localhost requests; it is neither a Canvas cookie nor a Notion credential. Never paste your Notion token into the extension.

## Run

In one terminal, keep the bridge running in the foreground:

```zsh
.venv/bin/python server.py
```

In another terminal:

```zsh
# Checks the bridge, Canvas session, Notion access/schema, and assignment discovery.
.venv/bin/python sync.py --validate

# Reads both services and reports proposed creates/updates; it never writes Notion.
.venv/bin/python sync.py --dry-run

# Performs one synchronization pass.
.venv/bin/python sync.py --sync
```

The validation report includes database reachability, integration authorization, every property/type/option failure, Canvas availability, assignments discovered, pages created, duplicates skipped, existing pages preserved, failures, and assignments that have no due date.

Canvas timestamps with `Z` or an explicit offset are converted as aware datetimes to `America/Los_Angeles`. The outgoing Notion date retains the same instant with a correct `-08:00` (PST) or `-07:00` (PDT) offset and 24-hour time. No due date becomes an empty Notion Date.

## Matching, updates, and privacy

The project does not add a hidden Notion property without your approval. The default `"duplicate_key_property": null` sends exactly the seven listed properties. When Canvas supplies `html_url`, that exact Canvas URL is the stable duplicate match. If Canvas lacks it but has an ID, the seven allowed properties cannot persist that ID, so the documented conservative fallback is Assignment + Course + Due Date. Multiple matching pages are reported as ambiguous and no extra page is created. If you explicitly approve a durable Canvas identity field, create a **Text** property such as `Canvas Sync Key`, set `"duplicate_key_property": "Canvas Sync Key"`, and the project writes `canvas:{course_id}:{assignment_id}` (or `canvas-url:{course_id}:{url}` only when the ID is unavailable) to it. A new page gets all seven requested properties including default Status, plus that explicitly approved internal field. Existing pages never have Status touched; updates only send Assignment, Course, Due Date, Time, Submission, and Link (and the approved key) when Canvas data differs. Pages removed from Canvas are never deleted.

Canvas uses paginated active-course and per-course assignment endpoints, with configured page and response limits. A session-expired 401/403 tells you to sign in again in Chrome; a disconnected extension error tells you to load/configure the extension and keep Chrome running. Notion 401 means token issue, 403 means permissions, 404 means wrong/not-shared database, 409 means retry, and 429/5xx are retried with exponential backoff.

No SQLite cache is created by this version. If you add SQLite/cache/logging later, treat it as private course data and keep it out of Git; `.gitignore` already excludes `*.sqlite3`, logs, virtual environments, secrets, and extension packages. Canvas response bodies are not logged by default.

## Tests

```zsh
.venv/bin/python -m pytest -q
.venv/bin/python -m py_compile *.py
.venv/bin/python -m json.tool extension/manifest.json >/dev/null
```

The tests mock Canvas and Notion and cover URL/method rejection, bridge authentication/expiry, Canvas pagination, normalization, PST/PDT, missing dates, duplicates, Status preservation, default Status, property payloads/schema failures, and Notion 401/403/404/409/429/5xx behavior. They do not require credentials.
