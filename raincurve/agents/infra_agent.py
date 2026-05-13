from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash, _truncate

MAX_TOOL_CALLS = 150
MAX_WALLCLOCK_S = 1200

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. Use for: docker run, docker ps, docker logs, "
            "docker exec, docker network, curl, and any other CLI tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout_s": {"type": "integer", "description": "Timeout in seconds. Default 60."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the project directory. Use to read .env files, "
            "docker-compose.yml, config files, source code."
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
        "name": "text_editor",
        "description": (
            "View or edit files. Commands: view (read a file with line numbers), "
            "create (write a new file), str_replace (replace text in a file). "
            "Use for creating scripts, Dockerfiles, config files, patching code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["view", "create", "str_replace"]},
                "path": {"type": "string", "description": "File path relative to project root"},
                "file_text": {"type": "string", "description": "For create: full file content"},
                "old_str": {"type": "string", "description": "For str_replace: exact text to find"},
                "new_str": {"type": "string", "description": "For str_replace: replacement text"},
                "view_range": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "For view: [start_line, end_line]",
                },
            },
            "required": ["command", "path"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch a URL. Use for reading documentation, setup guides, Docker Hub pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web. Use for finding Docker images, setup guides, troubleshooting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "delegate_task",
        "description": (
            "Spawn a sub-agent to handle one service. The sub-agent gets bash, "
            "read_file, and text_editor tools. You get back its result when it "
            "finishes. Use ONLY when you have 3+ independent services and want to "
            "save time. For 1-2 services, do them yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "instructions": {
                    "type": "string",
                    "description": (
                        "Specific instructions: what image to use, what docker run "
                        "command, what env vars to set, how to verify health."
                    ),
                },
            },
            "required": ["service_name", "instructions"],
        },
    },
    {
        "name": "done",
        "description": "Call when ALL infrastructure services are running and verified.",
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
                            "status": {
                                "type": "string",
                                "enum": ["running", "not_needed", "pipe", "disabled"],
                            },
                            "env_wiring": {"type": "object"},
                        },
                        "required": ["name", "container_name", "status"],
                    },
                },
                "env_vars_for_app": {
                    "type": "object",
                    "description": "ALL env vars the app container needs for these services",
                },
                "notes": {"type": "string"},
            },
            "required": ["services", "env_vars_for_app"],
        },
    },
]

SYSTEM_PROMPT = """\
You are the infrastructure agent. Your job: set up ALL external dependencies \
for this application — databases, caches, message brokers, object storage, and \
HTTP API services.

You are NOT building the app. You are NOT running the app. You are setting up \
infrastructure and providing env vars for the build agent.

## CRITICAL: Read project documentation FIRST

The project's README and setup docs are provided below. They describe what \
infrastructure this project needs — databases, services, environment variables, \
and how to configure them. Read this BEFORE scanning .env files or running \
commands. The README is the authoritative source for what the project requires.

{project_docs}

## Code Analysis Results

{code_context_summary}

## Docker Conventions

- Network: `{network_name}` (already exists)
- Container naming: `{container_name}-<service>` (e.g., `{container_name}-postgres`)
- Label all containers: `--label rc-aux-of={project_name}`
- Always: `--restart=unless-stopped`, memory and CPU limits

## Service Recipes (adapt based on .env files)

{recipes}

## Pipe — LLM-backed API mocking

{pipe_info}

## Disabled Services

{disabled_services}

## Your Approach

1. Read the project documentation above to understand what infrastructure is needed
2. Read .env / .env.example to get correct database names, credentials, URLs
3. Check what's already running: `docker ps --filter "label=rc-aux-of={project_name}"`
   - If a service is already running and healthy, collect its env vars and move on
4. Set up services in dependency order: databases → caches → brokers → HTTP APIs
5. For each service:
   a. If already running and healthy → collect env vars, skip
   b. If needs a container → start it, wait for health check, collect env vars
   c. If it's an HTTP API (Stripe, Twilio, SendGrid) → wire to Pipe, no container
   d. If it's analytics/monitoring → disable via env var
6. Call done with ALL services and ALL env vars

## delegate_task (optional)

If you have 3+ independent services to set up, you can use `delegate_task` to \
run them in parallel. Give specific instructions including the exact docker run \
command and expected env vars. Only delegate simple, well-understood services. \
Do complex or research-heavy services yourself.

## Rules

- NEVER run DROP DATABASE, DROP SCHEMA, or TRUNCATE
- If a service fails after 2 attempts, skip it and note it
- Verify each service with ONE health check (pg_isready, redis-cli ping, etc.)
- Do NOT write elaborate multi-line test scripts — a simple health check is enough
- Do NOT remove or stop containers that are already running
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
        pipe_info: str,
        disabled_services: str,
        project_docs: object | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.project_name = project_name
        self.container_name = container_name
        self.network_name = network_name
        self.code_context_summary = code_context_summary
        self.recipes_text = recipes_text
        self.pipe_info = pipe_info
        self.disabled_services = disabled_services
        self.project_docs = project_docs
        self._delegated_env_vars: dict[str, str] = {}

    def _format_project_docs(self) -> str:
        if not self.project_docs:
            return "(no project documentation found)"
        parts: list[str] = []
        for attr, label in [
            ("readme", "README"),
            ("claude_md", "CLAUDE.md"),
            ("contributing", "CONTRIBUTING"),
        ]:
            val = getattr(self.project_docs, attr, None)
            if val:
                parts.append(f"### {label}\n{val[:6000]}\n")
        return "\n".join(parts) if parts else "(no project documentation found)"

    def run(self) -> AgentResult:
        system = SYSTEM_PROMPT.format(
            code_context_summary=self.code_context_summary,
            network_name=self.network_name,
            container_name=self.container_name,
            project_name=self.project_name,
            recipes=self.recipes_text,
            pipe_info=self.pipe_info,
            disabled_services=self.disabled_services or "(none)",
            project_docs=self._format_project_docs(),
        )

        user_msg = (
            f"Set up all required infrastructure services for this project. "
            f"The project is at: {self.project_dir}\n\n"
            f"Read .env files first, check what's already running, "
            f"then start each missing service and call done."
        )

        result = self._run_loop(system, user_msg, TOOLS, self._handle_tool)

        if result.success and result.output and self._delegated_env_vars:
            env = result.output.get("env_vars_for_app", {})
            env.update(self._delegated_env_vars)
            result.output["env_vars_for_app"] = env

        return result

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            return self._handle_bash(args)
        elif name == "read_file":
            return self._handle_read_file(args)
        elif name == "text_editor":
            return self._handle_editor(args)
        elif name == "web_fetch":
            return self._handle_web_fetch(args)
        elif name == "web_search":
            return self._handle_web_search(args)
        elif name == "delegate_task":
            return self._handle_delegate(args)
        return f"Unknown tool: {name}"

    def _handle_bash(self, args: dict) -> str:
        cmd = args.get("command", "")
        if not cmd:
            return "Error: 'command' is required."
        timeout = args.get("timeout_s", 60)

        cmd_lower = cmd.lower()
        for pattern in ["drop database", "drop schema", "truncate "]:
            if pattern in cmd_lower:
                return "BLOCKED: Destructive database commands are forbidden."

        if any(p in cmd_lower for p in ("docker rm", "docker stop", "docker kill")):
            if self.container_name in cmd:
                return (
                    "BLOCKED: Do not destroy running project containers. "
                    "Debug with `docker logs` and `docker exec` instead."
                )

        self._log(f"$ {cmd[:200]}")
        r = _exec_bash(cmd, self.project_dir, timeout)
        out = r.stdout + (f"\nSTDERR:\n{r.stderr}" if r.stderr else "")
        return _truncate(f"exit_code={r.exit_code}\n{out or '(no output)'}")

    def _handle_read_file(self, args: dict) -> str:
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

    def _handle_editor(self, args: dict) -> str:
        command = args["command"]
        rel_path = args["path"]
        full_path = Path(self.project_dir) / rel_path

        if command == "view":
            if not full_path.exists():
                return f"File not found: {full_path}"
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except (PermissionError, OSError) as e:
                return f"Error reading: {e}"
            lines = content.splitlines()
            view_range = args.get("view_range")
            if view_range and len(view_range) == 2:
                start = max(0, view_range[0] - 1)
                lines = lines[start : view_range[1]]
                return "\n".join(f"{i + view_range[0]}: {line}" for i, line in enumerate(lines))
            return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))

        elif command == "create":
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(args.get("file_text", ""), encoding="utf-8")
            self._log(f"Created {rel_path}")
            return f"Created {full_path}"

        elif command == "str_replace":
            if not full_path.exists():
                return f"File not found: {full_path}"
            content = full_path.read_text(encoding="utf-8", errors="replace")
            old = args.get("old_str", "")
            if old not in content:
                return f"old_str not found in {full_path}"
            new = args.get("new_str", "")
            full_path.write_text(content.replace(old, new, 1), encoding="utf-8")
            self._log(f"Edited {rel_path}")
            return f"Replaced in {full_path}"

        return f"Unknown editor command: {command}"

    def _handle_web_fetch(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: no URL provided"
        self._log(f"Fetching: {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "raincurve/0.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read(200_000)
                text = raw.decode("utf-8", errors="replace")
            if "html" in content_type:
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text)
            return _truncate(text, head=8000, tail=4000)
        except Exception as e:
            return f"Fetch failed: {e}"

    def _handle_web_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return "Error: no query provided"
        self._log(f"Searching: {query}")
        encoded_q = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_q}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "raincurve/0.1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read(100_000).decode("utf-8", errors="replace")
            link_pattern = re.compile(
                r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL,
            )
            snippet_pattern = re.compile(
                r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL,
            )
            links = link_pattern.findall(html)
            snippets = snippet_pattern.findall(html)
            results = []
            for i, (u, title) in enumerate(links[:8]):
                title_clean = re.sub(r"<[^>]+>", "", title).strip()
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
                if "uddg=" in u:
                    match = re.search(r"uddg=([^&]+)", u)
                    if match:
                        u = urllib.parse.unquote(match.group(1))
                results.append(f"{i + 1}. {title_clean}\n   {u}\n   {snippet}")
            return "\n".join(results) if results else "No results found."
        except Exception as e:
            return f"Search failed: {e}"

    def _handle_delegate(self, args: dict) -> str:
        service_name = args.get("service_name", "")
        instructions = args.get("instructions", "")
        if not service_name or not instructions:
            return "Error: service_name and instructions are required."

        self._log(f"Delegating: {service_name}")

        sub = _InfraSubAgent(
            project_dir=self.project_dir,
            container_name=self.container_name,
            network_name=self.network_name,
            project_name=self.project_name,
            service_name=service_name,
            instructions=instructions,
            on_log=lambda m: self._log(f"[{service_name}] {m}"),
        )
        result = sub.run()

        if result.success and result.output:
            output = result.output
            env_vars = output.get("env_vars", {})
            if env_vars:
                self._delegated_env_vars.update(env_vars)
            self._log(
                f"[{service_name}] Done: {output.get('status', '?')} "
                f"({result.duration_s:.0f}s, {result.tool_call_count} calls)"
            )
            return json.dumps({
                "status": "success",
                "service": service_name,
                "container": output.get("container_name", ""),
                "env_vars": env_vars,
                "summary": output.get("summary", ""),
            })
        else:
            self._log(f"[{service_name}] Failed: {result.failure_reason}")
            return json.dumps({
                "status": "failed",
                "service": service_name,
                "reason": result.failure_reason or "unknown",
            })

    def _verify_done(self, done_output: dict) -> str | None:
        services = done_output.get("services", [])
        env_vars = done_output.get("env_vars_for_app", {})

        if not services and not env_vars:
            return "No services and no env vars provided. Did you skip everything?"

        active = [s for s in services if s.get("status") in ("running", "pipe")]
        if not active and services:
            statuses = [s.get("status", "?") for s in services]
            if not all(s in ("not_needed", "disabled") for s in statuses):
                return "No services are running or wired to Pipe. Verify health checks."

        return None


_SUB_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command (docker, curl, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {"type": "integer", "description": "Default 60."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a project file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative to project root"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "done",
        "description": "Report what you set up.",
        "input_schema": {
            "type": "object",
            "properties": {
                "container_name": {"type": "string"},
                "status": {"type": "string", "enum": ["running", "failed", "not_needed"]},
                "env_vars": {"type": "object", "description": "Env vars the app needs"},
                "summary": {"type": "string"},
            },
            "required": ["status", "summary"],
        },
    },
]

_SUB_PROMPT = """\
You are a focused infrastructure sub-agent. Set up ONE service: {service_name}.

Container naming: `{container_name}-{service_name}`
Network: `{network_name}`
Label: `--label rc-aux-of={project_name}`
Always: `--restart=unless-stopped`

## Instructions from parent agent:

{instructions}

## Rules
- Follow the instructions exactly
- Verify with ONE health check
- Call done with the container name, env vars, and status
- Do NOT write elaborate test scripts
- Do NOT remove or stop existing containers
"""


class _InfraSubAgent(BaseAgent):
    MAX_TOOL_CALLS = 30
    MAX_WALLCLOCK_S = 300

    def __init__(
        self,
        project_dir: str,
        container_name: str,
        network_name: str,
        project_name: str,
        service_name: str,
        instructions: str,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.container_name = container_name
        self.network_name = network_name
        self.project_name = project_name
        self.service_name = service_name
        self.instructions = instructions

    def run(self) -> AgentResult:
        system = _SUB_PROMPT.format(
            service_name=self.service_name,
            container_name=self.container_name,
            network_name=self.network_name,
            project_name=self.project_name,
            instructions=self.instructions,
        )
        user_msg = f"Set up {self.service_name} now."
        return self._run_loop(system, user_msg, _SUB_TOOLS, self._handle_tool)

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            cmd = args.get("command", "")
            if not cmd:
                return "Error: 'command' is required."
            timeout = args.get("timeout_s", 60)
            self._log(f"$ {cmd[:200]}")
            r = _exec_bash(cmd, self.project_dir, timeout)
            out = r.stdout + (f"\nSTDERR:\n{r.stderr}" if r.stderr else "")
            return _truncate(f"exit_code={r.exit_code}\n{out or '(no output)'}")

        elif name == "read_file":
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
