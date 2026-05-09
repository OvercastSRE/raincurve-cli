from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash, _truncate
from raincurve.agents.schema_extractor import extract_db_schema, extract_api_routes

MAX_TOOL_CALLS = 100
MAX_WALLCLOCK_S = 1200

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. Use for: psql against the database container "
            "(via docker exec), curl against the running app, and reading files "
            "in the project directory to understand the data model. Also use for "
            "reading route handlers, model definitions, migration files, and "
            "seed scripts to understand how data flows through the app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {"type": "integer"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file. Use this to create seed scripts (.py or .sql). "
            "Much more reliable than echoing content via bash, especially for large scripts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to write"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "done",
        "description": (
            "Call when you've seeded enough data that the app looks like 10 real "
            "users have been actively using it for a month. Provide verify_routes "
            "for the harness to check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verify_routes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "URL paths to curl as the logged-in user to confirm seeding "
                        "worked. E.g. ['/api/websites', '/dashboard']. Pick endpoints "
                        "the app's main UI uses — list views, dashboards, feeds. "
                        "Skip health/heartbeat endpoints."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": "1-2 sentences: what data you seeded and how.",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "User-visible summary, e.g. 'Seeded 8 websites with 2400 "
                        "pageview events across 10 users over 30 simulated days'."
                    ),
                },
                "modifications": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files modified to enable seeding (e.g. disabled rate limit).",
                },
            },
            "required": ["verify_routes", "rationale", "summary"],
        },
    },
]

SYSTEM_PROMPT = """\
You are the data seeder for a production sandbox. Your directive:

  IMAGINE 10 REAL USERS HAVE BEEN ACTIVELY USING THIS APP FOR A MONTH.

That's the state you need to create. Not empty tables with one admin. Not random \
garbage rows. A living, breathing application with realistic data that tells a story \
— users who signed up on different days, created projects/resources, generated \
activity over time, and left the kind of data trail that real usage produces.

## Tools

  bash — run shell commands. Use for:
    - psql against the database (via `docker exec {db_container} psql -U {db_user} -d {db_name} -c "..."`)
    - curl against the running app at http://localhost:{app_port}
    - Executing seed scripts you've written

  write_file — write content to a file. Use this for seed scripts instead of \
    echoing via bash. Supports large files reliably.

  done — declare completion with verify_routes the harness will check.

## Environment

  - Database container: {db_container}
  - Database credentials: user={db_user}, database={db_name}
  - Docker network: {network}
  - App accessible at: http://localhost:{app_port}
  - Project directory: {project_dir}
  - Login credentials: {login_info}

## Database Schema (already extracted — DO NOT re-read with \\dt or \\d+)

{schema_context}

## API Routes

{routes_context}

## Approach — CHECK EXISTING DATA FIRST, THEN SEED OR CALL DONE

FIRST: Run 2-3 COUNT(*) queries on the main tables to check if seed data already \
exists. Many apps seed data during their own startup process.

IF DATA ALREADY EXISTS (tables have rows): The app has its own seed data. \
Just spread timestamps across the last 30 days to make it look realistic, then \
call done. Do NOT spend 20+ tool calls re-reading the schema — you already have it.

IF DATA IS EMPTY: Seed it. The schema and routes are above. Do NOT run \\dt, \\d+, \
or read migration files — that wastes tool calls. Jump straight to seeding.

  1. SEED VIA THE API FIRST. Login as the primary user, capture the auth token, \
     then use the app's own create endpoints. This is critical because:
     - Data flows through business logic (FKs, validation, derived fields all correct)
     - Timestamps, UUIDs, slugs are generated properly
     - Any post-create hooks fire (search indexing, counters, etc.)
     - The data WILL ACTUALLY BE VISIBLE in the app's UI

  2. SEED VIA SQL FOR VOLUME DATA. After creating the structural entities via API \
     (users, projects, websites), use SQL for high-volume data (events, pageviews, \
     logs, messages) where the API would be too slow. But ALWAYS reference real PKs \
     from step 1 — NEVER use random UUIDs for foreign key columns.

  3. MAKE IT REALISTIC:
     - Vary timestamps across the last 30 days (not all NOW())
     - Use realistic names, emails, URLs (not test1, test2, test3)
     - Create different amounts of data per user (power users vs casual)
     - Include some edge cases naturally (empty projects, users with no activity)

  4. For large SQL inserts, use write_file to create a .sql file, then pipe it in: \
     `docker exec -i {db_container} psql -U {db_user} -d {db_name} < /path/to/seed.sql`

## CRITICAL: FK Integrity

  ALL volume data (sessions, events, pageviews) MUST reference IDs that actually \
  exist in parent tables (websites, users). If you insert sessions for a website_id \
  that doesn't exist in the website table, the data will be INVISIBLE in the app. \
  Always create parent records first (via API), capture their IDs, then use those \
  real IDs in bulk SQL inserts.

## Quantity targets

  - ~10 users (the primary/admin user + 9 others with varying activity levels)
  - ~15-25 root entities per core feature (websites, projects, boards, etc.)
  - ~100-500 second-level entities (sessions, posts, comments, etc.)
  - ~1000-3000 activity records (events, pageviews, messages, etc.)
  - Spread across the last 30 days with realistic distribution (more recent = more activity)

## Postgres tips

  - Random element from an array: `(ARRAY['a','b','c'])[1 + floor(random() * 3)::int]`
  - Generate UUIDs: `gen_random_uuid()`
  - Date spread: `NOW() - (random() * INTERVAL '30 days')`
  - Bulk insert with generate_series: `INSERT INTO ... SELECT ... FROM generate_series(1, 100)`

## Verification

  When you call done, provide 3-5 verify_routes — the URL paths that the app's \
  main list/dashboard views use. The harness will curl each one and check for \
  non-empty responses. Pick the routes where seeded data should be VISIBLE.

You have 15 minutes. The schema is above — go seed.
"""


class SeederAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S
    MAX_TOKENS = 16384
    MODEL_OVERRIDE = None

    def __init__(
        self,
        project_dir: str,
        app_port: int,
        db_container: str | None = None,
        db_user: str | None = None,
        db_name: str | None = None,
        network: str = "",
        login_info: str = "",
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.app_port = app_port
        self.db_container = db_container or ""
        self.db_user = db_user or "postgres"
        self.db_name = db_name or "app"
        self.network = network
        self.login_info = login_info

    def run(self) -> AgentResult:
        self._log("Extracting database schema...")
        schema_context = extract_db_schema(
            self.project_dir, self.db_container, self.db_user, self.db_name
        )
        self._log("Discovering API routes...")
        routes_context = extract_api_routes(self.project_dir, self.app_port)

        if len(schema_context) > 12000:
            schema_context = schema_context[:12000] + "\n\n[... truncated ...]"
        if len(routes_context) > 4000:
            routes_context = routes_context[:4000] + "\n\n[... truncated ...]"

        self._log(f"Schema: {len(schema_context)} chars, Routes: {len(routes_context)} chars")
        self._log("Starting seed agent...")

        system = SYSTEM_PROMPT.format(
            project_dir=self.project_dir,
            app_port=self.app_port,
            db_container=self.db_container,
            db_user=self.db_user,
            db_name=self.db_name,
            network=self.network,
            login_info=self.login_info,
            schema_context=schema_context or "(no database detected)",
            routes_context=routes_context or "(no routes discovered)",
        )

        user_msg = (
            f"Seed this application so it looks like 10 real users have been actively "
            f"using it for a month. The app is running at http://localhost:{self.app_port}.\n\n"
            f"The database schema and API routes are already in your system prompt. "
            f"Do NOT re-read them — jump straight to seeding via API + SQL.\n\n"
            f"The project is at: {self.project_dir}"
        )

        return self._run_loop(system, user_msg, TOOLS, self._handle_tool)

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            cmd = args.get("command", "")
            if not cmd:
                return "Error: 'command' argument is required."
            timeout = args.get("timeout_s", 60)
            self._log(f"$ {cmd[:120]}")
            r = _exec_bash(cmd, self.project_dir, timeout)
            out = r.stdout + (f"\nSTDERR:\n{r.stderr}" if r.stderr else "")
            return _truncate(f"exit_code={r.exit_code}\n{out or '(no output)'}")
        elif name == "write_file":
            path = args.get("path") or args.get("file_path", "")
            content = args.get("content") or args.get("file_content") or args.get("text", "")
            if not path:
                return "Error: 'path' argument is required."
            if not content:
                return "Error: 'content' argument is required. Provide the full file content."
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(content, encoding="utf-8")
                self._log(f"Wrote {path} ({len(content)} bytes)")
                return f"OK — wrote {len(content)} bytes to {path}"
            except Exception as e:
                return f"Error writing file: {e}"
        return f"Unknown tool: {name}"

    def _verify_done(self, done_output: dict) -> str | None:
        routes = done_output.get("verify_routes", [])
        if not routes:
            return "No verify_routes provided."

        failures: list[str] = []
        for route in routes:
            url = f"http://localhost:{self.app_port}{route}"
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read()
                    if len(body) < 50:
                        failures.append(
                            f"{route} — response too small ({len(body)} bytes), "
                            f"data may not be visible"
                        )
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                failures.append(f"{route} — failed: {e}")

        if failures:
            return (
                "Some verify_routes returned empty or failed:\n"
                + "\n".join(failures)
                + "\n\nThis means the seeded data is not yet visible through the app's "
                "API. Check if you need to login first, or if the data was inserted "
                "correctly."
            )

        seed_script = Path(self.project_dir) / "_rc_seed.py"
        if seed_script.exists():
            seed_script.unlink()

        return None
