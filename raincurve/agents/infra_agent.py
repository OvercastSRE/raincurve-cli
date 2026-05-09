from __future__ import annotations

from typing import Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash, _truncate

MAX_TOOL_CALLS = 100
MAX_WALLCLOCK_S = 600

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. Use for: docker run, docker ps, docker logs, "
            "docker exec, docker network commands. Also use to read .env files "
            "and verify service connectivity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout_s": {"type": "integer", "description": "Timeout in seconds. Default 30."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the project directory. Use to read .env files, "
            "docker-compose.yml, config files to understand service requirements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "done",
        "description": "Call when all infrastructure services are running and verified.",
        "input_schema": {
            "type": "object",
            "properties": {
                "services": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "container_name": {"type": "string"},
                            "image": {"type": "string"},
                            "status": {"type": "string"},
                            "env_wiring": {
                                "type": "object",
                                "description": "Env vars to set on the app container for this service",
                            },
                        },
                        "required": ["name", "container_name", "status"],
                    },
                },
                "env_vars_for_app": {
                    "type": "object",
                    "description": "All env vars the app container should have for these services",
                },
                "notes": {"type": "string"},
            },
            "required": ["services", "env_vars_for_app"],
        },
    },
]

SYSTEM_PROMPT = """\
You are the infrastructure agent. Your job is to set up ALL external dependencies \
for this application: databases, caches, message brokers, and HTTP API services.

You are NOT building the app. You are NOT patching code. You are setting up the \
infrastructure and providing env vars for the build agent.

## Code Analysis Results

{code_context_summary}

## Project Environment Files

Read the project's .env files FIRST to get the correct database names, credentials, \
and connection URLs. The .env file is the source of truth — use the values from it, \
not recipe defaults.

## Docker Conventions

- Network: `{network_name}` (already exists)
- Container naming: `{container_name}-<service>` (e.g., `{container_name}-postgres`)
- Label all containers: `--label rc-aux-of={project_name}`
- Always set `--restart=unless-stopped`
- Always set memory and CPU limits

## Service Recipes (REFERENCE ONLY — adapt as needed)

These are pre-baked docker run commands. Use them as a STARTING POINT but \
adapt based on what you learn from the .env and code analysis. For example, \
if the recipe says POSTGRES_DB=app but the .env says PG_DATABASE_NAME=default, \
use default.

{recipes}

## Pipe — LLM-backed API mocking

{already_handled}

For HTTP API services (Stripe, Twilio, SendGrid, Resend, Postmark, etc.), you \
do NOT need to start a Docker container. Instead, point the SDK's base URL at \
Pipe. Pipe will generate realistic API responses using an LLM.

To wire a service to Pipe, include the base URL env var in your env_vars_for_app:
- STRIPE_API_BASE=http://host.docker.internal:<pipe_port>/stripe
- RESEND_API_BASE=http://host.docker.internal:<pipe_port>/resend
- TWILIO_API_BASE=http://host.docker.internal:<pipe_port>/twilio
etc.

Also include any API key env vars the SDK needs (use fake keys like sk_test_pipe).

## Your Approach

1. Read the project's .env file (docker-compose .env, .env.example, etc.) to find:
   - Database name, credentials, connection URL
   - Redis URL
   - Which services are enabled/disabled
   - Any other config

2. For infrastructure services (databases, caches, queues):
   - Start the container with values from the .env file
   - Wait for health check
   - Verify connectivity

3. For HTTP API services (Stripe, Twilio, SendGrid, etc.):
   - Wire them to Pipe via env vars (no container needed)
   - Include both the base URL and API key env vars

4. For services marked "NOT USED" in the code analysis:
   - SKIP them entirely

5. For analytics/monitoring services (Sentry, PostHog, Datadog):
   - Disable via env var (e.g., SENTRY_DSN="")

6. Call done with ALL services and ALL env vars the app needs

## Rules

- NEVER run DROP DATABASE, DROP SCHEMA, or TRUNCATE
- Read .env files FIRST — they have the correct config
- If a service fails after 2 attempts, skip it and note it
- Include ALL env vars in env_vars_for_app — the build agent uses this directly
"""


class InfraAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S

    def __init__(
        self,
        project_dir: str,
        project_name: str,
        container_name: str,
        network_name: str,
        code_context_summary: str,
        recipes_text: str,
        already_handled: str,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.project_name = project_name
        self.container_name = container_name
        self.network_name = network_name
        self.code_context_summary = code_context_summary
        self.recipes_text = recipes_text
        self.already_handled = already_handled

    def run(self) -> AgentResult:
        system = SYSTEM_PROMPT.format(
            code_context_summary=self.code_context_summary,
            network_name=self.network_name,
            container_name=self.container_name,
            project_name=self.project_name,
            recipes=self.recipes_text,
            already_handled=self.already_handled,
        )

        user_msg = (
            f"Start all required infrastructure services for this project. "
            f"The project is at: {self.project_dir}\n\n"
            f"Read the .env files first to get the correct database name and credentials. "
            f"Then start each service, verify it's healthy, and call done."
        )

        return self._run_loop(system, user_msg, TOOLS, self._handle_tool)

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            cmd = args.get("command", "")
            if not cmd:
                return "Error: 'command' is required."
            timeout = args.get("timeout_s", 30)

            cmd_lower = cmd.lower()
            for pattern in ["drop database", "drop schema", "truncate "]:
                if pattern in cmd_lower:
                    return (
                        "BLOCKED: Destructive database commands are forbidden. "
                        "If the database has issues, recreate the container."
                    )

            self._log(f"$ {cmd[:200]}")
            r = _exec_bash(cmd, self.project_dir, timeout)
            out = r.stdout + (f"\nSTDERR:\n{r.stderr}" if r.stderr else "")
            return _truncate(f"exit_code={r.exit_code}\n{out or '(no output)'}")

        elif name == "read_file":
            from pathlib import Path

            rel_path = args.get("path", "")
            if not rel_path:
                return "Error: 'path' is required."
            full_path = Path(self.project_dir) / rel_path
            if not full_path.exists():
                return f"File not found: {rel_path}"
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
                if len(content) > 8000:
                    content = content[:8000] + "\n[... truncated ...]"
                return content
            except (PermissionError, OSError) as e:
                return f"Error reading {rel_path}: {e}"

        return f"Unknown tool: {name}"

    def _verify_done(self, done_output: dict) -> str | None:
        services = done_output.get("services", [])
        env_vars = done_output.get("env_vars_for_app", {})

        if not services and not env_vars:
            return "No services started and no env vars provided. Did you skip everything?"

        running = [s for s in services if s.get("status") == "running"]
        if not running and services:
            return (
                "No services are marked as 'running'. Verify each service is healthy "
                "before calling done."
            )

        return None
