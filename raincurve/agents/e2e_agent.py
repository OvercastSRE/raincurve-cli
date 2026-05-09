from __future__ import annotations

from typing import Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash, _truncate

MAX_TOOL_CALLS = 80
MAX_WALLCLOCK_S = 600

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. Use for: curl against the running app, "
            "jq to parse JSON responses, docker exec to inspect state. "
            "Returns stdout, stderr, exit_code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "timeout_s": {"type": "integer", "description": "Timeout in seconds. Default 15."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "done",
        "description": "Report E2E test results. Call after running all journeys.",
        "input_schema": {
            "type": "object",
            "properties": {
                "journeys": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "method": {"type": "string"},
                                        "path": {"type": "string"},
                                        "status": {"type": "integer"},
                                        "passed": {"type": "boolean"},
                                        "note": {"type": "string"},
                                    },
                                    "required": ["method", "path", "passed"],
                                },
                            },
                            "passed": {"type": "boolean"},
                        },
                        "required": ["name", "steps", "passed"],
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["journeys", "summary"],
        },
    },
]

SYSTEM_PROMPT = """\
You are an end-to-end test agent. The sandbox is fully running and seeded with \
data. Your job: drive multi-step user journeys through the app and verify they \
actually work — not just 200 OK, but correct response bodies, data flowing \
through, state changing as expected.

## Environment

- App: http://localhost:{app_port}
- Login: {login_info}
- Project directory: {project_dir}

## API routes discovered

{routes_context}

## Instructions

Run 3-5 realistic user journeys. Each journey is a SEQUENCE of HTTP calls that \
a real user would make, where each step depends on the previous one.

Example journeys (adapt to this app's actual features):

**Authentication flow**:
  POST /api/auth/login → get token → GET /api/me with token → verify user data

**CRUD lifecycle**:
  POST /api/items (create) → GET /api/items/:id (verify exists) → \
  PUT /api/items/:id (update) → GET /api/items/:id (verify updated) → \
  DELETE /api/items/:id → GET /api/items/:id (verify 404)

**Multi-resource flow**:
  Create parent → create child referencing parent → list children → \
  verify parent-child relationship

## Rules

1. Start EVERY session by logging in (if the app has auth) and capturing the \
   auth token/cookie. Use it for all subsequent requests.
2. At each step, check BOTH the status code AND the response body. A 200 with \
   an empty body or error message is a FAILURE.
3. Use the IDs returned from create operations in subsequent steps — don't \
   hardcode IDs.
4. Use curl with -s -w "\\nHTTP_STATUS:%{{http_code}}" to capture both body \
   and status in one call.
5. If a journey fails at a step, note which step and WHY, then move on to the \
   next journey. Don't get stuck debugging one failure.
6. Test the app's CORE features — the main thing users do, not edge cases.

## Call done with

- journeys: array of journey results with step details
- summary: "3/5 journeys passed" (or similar)
"""


class E2EAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S

    def __init__(
        self,
        project_dir: str,
        app_port: int,
        login_info: str = "",
        routes_context: str = "",
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.app_port = app_port
        self.login_info = login_info
        self.routes_context = routes_context

    def run(self) -> AgentResult:
        system = SYSTEM_PROMPT.format(
            project_dir=self.project_dir,
            app_port=self.app_port,
            login_info=self.login_info or "No credentials provided — check README or seed data",
            routes_context=self.routes_context or "(no routes discovered — explore the API)",
        )

        user_msg = (
            f"Run end-to-end user journey tests against http://localhost:{self.app_port}. "
            f"Drive 3-5 multi-step flows, verify each step's response body, and report results."
        )

        return self._run_loop(system, user_msg, TOOLS, self._handle_tool)

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            cmd = args.get("command", "")
            if not cmd:
                return "Error: 'command' argument is required."
            timeout = args.get("timeout_s", 15)
            self._log(f"$ {cmd[:120]}")
            r = _exec_bash(cmd, self.project_dir, timeout)
            out = r.stdout + (f"\nSTDERR:\n{r.stderr}" if r.stderr else "")
            return _truncate(f"exit_code={r.exit_code}\n{out or '(no output)'}")
        return f"Unknown tool: {name}"

    def _verify_done(self, done_output: dict) -> str | None:
        journeys = done_output.get("journeys", [])
        if not journeys:
            return "No journeys tested. Run at least 3 user journeys."
        if len(journeys) < 2:
            return f"Only {len(journeys)} journey tested. Run at least 3."
        passed = sum(1 for j in journeys if j.get("passed"))
        total = len(journeys)
        if passed == 0:
            return (
                f"All {total} journeys failed. At least one must pass. "
                f"Check if you're logging in correctly and using the right endpoints."
            )
        return None
