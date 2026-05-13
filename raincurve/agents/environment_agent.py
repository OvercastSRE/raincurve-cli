from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash, _truncate
from raincurve.utils.repo_scanner import build_repo_context

MAX_TOOL_CALLS = 80
MAX_WALLCLOCK_S = 600

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command in the project directory. Use for: git, docker, "
            "npm/pip/cargo install, running build commands, inspecting files, "
            "starting services, checking health. Returns stdout, stderr, exit_code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory (relative to project root). Defaults to project root.",
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default 120.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "text_editor",
        "description": (
            "View or edit files. Commands: "
            "view (read a file or line range), "
            "create (write a new file), "
            "str_replace (replace exact text in a file)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace"],
                },
                "path": {"type": "string", "description": "File path relative to project root"},
                "file_text": {"type": "string", "description": "For create: full file content"},
                "old_str": {"type": "string", "description": "For str_replace: exact text to find"},
                "new_str": {"type": "string", "description": "For str_replace: replacement text"},
                "view_range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "For view: [start_line, end_line]",
                },
            },
            "required": ["command", "path"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a URL and return its content. Use this to research how to run "
            "external services locally — e.g., find Docker images, local mock projects, "
            "setup instructions for services like Supabase, MinIO, localstripe, etc. "
            "Works with GitHub READMEs, Docker Hub, blog posts, and documentation sites."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (e.g., a GitHub README, Docker Hub page, or docs page)",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for information. Use this to find how to mock or run "
            "external services locally — e.g., 'run supabase locally docker', "
            "'localstripe stateful stripe mock', 'minio s3 docker setup'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "done",
        "description": (
            "Call this when the application is built, running, and ALL verification checks pass. "
            "You must provide evidence that the database has tables, mock services respond, "
            "and at least one end-to-end API test passes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "description": "The host port the app is accessible on"},
                "health_path": {"type": "string", "description": "HTTP path to verify health"},
                "services": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "container_name": {"type": "string"},
                            "host_port": {"type": "integer"},
                            "image": {"type": "string"},
                        },
                    },
                    "description": "All Docker containers/services started",
                },
                "verification": {
                    "type": "object",
                    "description": "Evidence that the system actually works end-to-end",
                    "properties": {
                        "db_check": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "description": "The docker exec command run to check the database",
                                },
                                "tables_found": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "success": {"type": "boolean"},
                            },
                            "required": ["command", "tables_found", "success"],
                        },
                        "mock_checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "service": {"type": "string"},
                                    "url_tested": {"type": "string"},
                                    "status": {"type": "integer"},
                                    "success": {"type": "boolean"},
                                },
                                "required": ["service", "url_tested", "success"],
                            },
                            "description": "Result of testing each mock service endpoint",
                        },
                        "api_test": {
                            "type": "object",
                            "properties": {
                                "endpoint": {"type": "string"},
                                "method": {"type": "string"},
                                "status": {"type": "integer"},
                                "response_snippet": {
                                    "type": "string",
                                    "description": "First 500 chars of response body",
                                },
                                "success": {"type": "boolean"},
                            },
                            "required": ["endpoint", "method", "success"],
                        },
                    },
                    "required": ["db_check", "mock_checks", "api_test"],
                },
                "seed_commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Commands to seed data",
                },
                "test_credentials": {
                    "type": "object",
                    "description": "Default login credentials if the app has auth",
                    "properties": {
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                        "login_path": {"type": "string"},
                        "login_body_template": {"type": "object"},
                    },
                },
                "env_vars_used": {
                    "type": "object",
                    "description": "Environment variables set on containers",
                },
                "modifications": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files created or modified",
                },
                "notes": {"type": "string"},
            },
            "required": ["port", "health_path", "services", "verification"],
        },
    },
]

SYSTEM_PROMPT = """\
You are the build agent. Get the application running in Docker and verify it responds.

## CRITICAL: Follow project documentation

The project's README and setup docs are provided below. These contain the \
authoritative instructions for how to build and run this project. Follow them \
first — do NOT guess build commands or invent Dockerfiles when the project \
already documents how to set up.

## Context

- Docker network: `{network_name}`
- App container name: `{container_name}`
- Container label: `--label rc-aux-of={project_name}`
- Project directory: `{project_dir}`
- Use `--restart=unless-stopped` on the app container
- Include ALL environment variables from the context below in your docker run command
- If infrastructure (postgres, redis) is already running, do not recreate it
- If infrastructure is missing and the app needs it, start it yourself

## What you can do

- Build Docker images, write Dockerfiles, pull images
- Bind-mount source code into containers and run dev servers
- Start any service container the app needs
- Modify application code inside containers (this is a sandbox, not production)
- Run migrations, seed data, install dependencies
- Use web_search/web_fetch to research setup instructions

## When you're done

Call `done` with: port, health_path, services list, verification evidence, \
test_credentials (username, password, login_path), and modifications list.

Verify before calling done: health check passes, app logs show no crash loop.

## Rules

- Never run DROP DATABASE, DROP SCHEMA, or TRUNCATE
- Never modify files on the host — only inside containers or in build artifacts
- If a build fails, read the error and fix the cause before retrying
- Be patient with app startup — migrations can take 60-120 seconds

{project_docs}

{repo_context}
"""


class EnvironmentAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S

    def __init__(
        self,
        project_dir: str,
        project_name: str,
        container_name: str,
        network_name: str,
        env_overrides: dict[str, str] | None = None,
        detection_result: object | None = None,
        repo_brief: object | None = None,
        on_log: Callable[[str], None] | None = None,
        pre_started_services: set[str] | None = None,
        pipe_handled_services: set[str] | None = None,
        retry_context: str = "",
        strategy_directive: str = "",
        project_docs: object | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.project_name = project_name
        self.container_name = container_name
        self.network_name = network_name
        self.env_overrides = env_overrides or {}
        self.detection_result = detection_result
        self.repo_brief = repo_brief
        self.pre_started_services = pre_started_services or set()
        self.pipe_handled_services = pipe_handled_services or set()
        self.retry_context = retry_context
        self.strategy_directive = strategy_directive
        self.project_docs = project_docs
        self._build_failures: dict[str, int] = {}
        self._last_build_error: str = ""

    def _build_brief_message(self) -> str:
        """Build a targeted user message from the pre-analysis brief."""
        b = self.repo_brief
        parts = [
            f"Get this project running in Docker. The project is at: {self.project_dir}\n",
            f"Docker network '{self.network_name}' already exists — use it for all containers.\n",
            "\n## Pre-Analysis (already done for you — do NOT re-read these files)\n",
        ]

        # Stack
        if b.language != "unknown":
            line = f"- **Stack**: {b.language}"
            if b.framework:
                line += f" / {b.framework}"
            if b.language_version:
                line += f" (v{b.language_version})"
            if b.package_manager:
                line += f" — package manager: {b.package_manager}"
            parts.append(line)

        # Dockerfile
        if b.has_dockerfile:
            parts.append(f"- **Dockerfile**: exists at `{b.dockerfile_path}`")
            if b.dockerfile_analysis:
                da = b.dockerfile_analysis
                parts.append(f"  - Base image: `{da.base_image}`, Ports: {da.exposed_ports}, Stages: {da.stages}")
                if da.cmd:
                    parts.append(f"  - CMD: `{da.cmd}`")
            parts.append("  - USE this Dockerfile. Do not write a new one unless the build fails.")
        else:
            parts.append("- **No Dockerfile found** — you need to write one.")

        # Compose
        if b.has_compose and b.compose_analysis:
            ca = b.compose_analysis
            svc_names = [s.name for s in ca.services]
            parts.append(f"- **docker-compose.yml**: exists at `{b.compose_path}`")
            parts.append(f"  - Services: {', '.join(svc_names)}")
            parts.append(
                "  - TRY `docker compose up -d` first. If it fails, read the error and fix it. "
                "This is faster than setting up each service manually."
            )

        # Database
        if b.database_type:
            parts.append(f"- **Database**: {b.database_type}")
            if b.database_url_pattern:
                parts.append(f"  - URL pattern: `{b.database_url_pattern}`")

        # Migrations
        if b.migration_tool:
            parts.append(f"- **Migrations**: {b.migration_tool} — run `{b.migration_command}`")
        if b.seed_command:
            parts.append(f"- **Seed**: `{b.seed_command}`")

        # Port
        if b.app_port:
            parts.append(f"- **App port**: {b.app_port}")

        # Start/build commands
        if b.build_command:
            parts.append(f"- **Build command**: `{b.build_command}`")
        if b.start_command:
            parts.append(f"- **Start command**: `{b.start_command}`")

        # Services — merge from brief and from detection_result
        from raincurve.services.recipes import (
            get_recipe, build_docker_run_cmd, DISABLEABLE_SERVICES,
        )

        all_svc_names: list[str] = []
        if self.detection_result:
            for svc in self.detection_result.detected_services:
                all_svc_names.append(svc.name)
        # Also add from repo_brief.detected_services
        for s in b.detected_services:
            if s.name not in [n for n in all_svc_names]:
                all_svc_names.append(s.name)

        required = [n for n in all_svc_names if n not in DISABLEABLE_SERVICES]
        disableable = [n for n in all_svc_names if n in DISABLEABLE_SERVICES]

        already_handled = self.pre_started_services | self.pipe_handled_services
        needs_agent = [n for n in required if n not in already_handled]

        if self.pre_started_services or self.pipe_handled_services:
            parts.append(
                "\n## ALREADY RUNNING services (do NOT start these — they are live)\n"
                "These services are already running and their env vars are in the "
                "environment variables section below. Do NOT start containers for them. "
                "Just make sure the app code points at them (patch SDK base URLs if needed)."
            )
            for svc_name in sorted(self.pre_started_services):
                svc_container = f"{self.container_name}-{svc_name}"
                parts.append(f"- **{svc_name}** — container `{svc_container}` is running")
            for svc_name in sorted(self.pipe_handled_services):
                parts.append(
                    f"- **{svc_name}** — handled by Pipe (LLM mock at "
                    f"`host.docker.internal:19877/{svc_name}`). No container needed."
                )

        if needs_agent:
            parts.append(
                "\n## Services that NEED setup\n"
                "These are not yet running. Set up a working local replacement for each."
            )
            for svc_name in needs_agent:
                recipe = get_recipe(svc_name)
                if recipe:
                    cmd = build_docker_run_cmd(
                        recipe, self.container_name, self.network_name, self.project_name,
                    )
                    parts.append(f"\n**{svc_name}** — pre-baked recipe available:")
                    parts.append(f"```\n{cmd}\n```")
                    if recipe.env_wiring:
                        wiring = ", ".join(
                            f"`{k}={v}`" for k, v in recipe.env_wiring.items()
                        )
                        parts.append(f"Set on app container: {wiring}")
                else:
                    parts.append(
                        f"\n**{svc_name}** — no pre-baked recipe. Use `web_search` "
                        f"to find the right local replacement or mock for {svc_name}."
                    )

        # SDK import locations
        if self.detection_result and self.detection_result.import_hits:
            parts.append("\n## SDK import locations (where to find and patch SDK initialization)\n")
            parts.append(
                "These files import external service SDKs. When you need to patch "
                "SDK client initialization to point at a local mock, check these files first.\n"
            )
            for svc_name, files in self.detection_result.import_hits.items():
                file_list = ", ".join(f"`{f}`" for f in files[:5])
                parts.append(f"- **{svc_name}**: {file_list}")

        if disableable:
            parts.append("\n## Optional services (can be disabled)")
            for svc_name in disableable:
                parts.append(f"- **{svc_name}**: disable via environment variable if no local replacement exists")

        # Env overrides
        if self.env_overrides:
            parts.append("\n## Environment variables (provided by user)")
            for k, v in self.env_overrides.items():
                parts.append(f"  {k}={v}")

        # Key file contents (for agent to reference without reading)
        if b.key_file_contents:
            parts.append("\n## Key file contents (already read)")
            for fname, content in list(b.key_file_contents.items())[:8]:
                truncated = content[:3000] if len(content) > 3000 else content
                parts.append(f"\n### {fname}\n```\n{truncated}\n```")

        return "\n".join(parts)

    def _format_project_docs(self) -> str:
        if not self.project_docs:
            return ""
        parts: list[str] = ["## Project Documentation\n"]
        for attr, label in [
            ("readme", "README"),
            ("claude_md", "CLAUDE.md"),
            ("contributing", "CONTRIBUTING"),
            ("cursor_rules", ".cursorrules"),
        ]:
            val = getattr(self.project_docs, attr, None)
            if val:
                parts.append(f"### {label}\n{val[:6000]}\n")
        for name, content in (getattr(self.project_docs, "custom_docs", None) or {}).items():
            parts.append(f"### {name}\n{content[:3000]}\n")
        return "\n".join(parts) if len(parts) > 1 else ""

    def run(self) -> AgentResult:
        repo_context = build_repo_context(self.project_dir)
        docs_text = self._format_project_docs()
        system = SYSTEM_PROMPT.format(
            project_dir=self.project_dir,
            project_name=self.project_name,
            container_name=self.container_name,
            network_name=self.network_name,
            project_docs=docs_text,
            repo_context=repo_context,
        )

        if self.strategy_directive:
            system += self.strategy_directive

        if self.repo_brief:
            user_msg = self._build_brief_message()
        else:
            user_msg = (
                f"Get this project running in Docker. The project is at: {self.project_dir}\n\n"
                f"Docker network '{self.network_name}' already exists — use it for all containers.\n"
            )
            if self.detection_result and self.detection_result.detected_services:
                svc_lines = []
                for svc in self.detection_result.detected_services:
                    env_hint = ", ".join(svc.env_vars[:4])
                    svc_lines.append(f"- {svc.name} ({svc.description}): env vars [{env_hint}]")
                if self.detection_result.import_hits:
                    svc_lines.append("\nImport locations:")
                    for svc_name, files in self.detection_result.import_hits.items():
                        svc_lines.append(f"  {svc_name}: {', '.join(files[:3])}")
                detected_services_text = "\n".join(svc_lines)
                user_msg += (
                    "\n## External services detected in this codebase\n\n"
                    "You MUST set up local replacements for these. Use `web_search` and `web_fetch` "
                    "to find the right Docker images and setup instructions. Do NOT skip them.\n\n"
                    f"{detected_services_text}\n"
                )
            if self.env_overrides:
                user_msg += "\nThe user provided these environment variables:\n"
                for k, v in self.env_overrides.items():
                    user_msg += f"  {k}={v}\n"

        if self.pre_started_services:
            user_msg += (
                "\n## Infrastructure already running (set up by infra agent)\n\n"
                "These containers are ALREADY RUNNING on the Docker network. "
                "Do NOT recreate them. Just use them via their container hostnames.\n"
            )
            for svc in sorted(self.pre_started_services):
                user_msg += f"  - {self.container_name}-{svc}\n"
            user_msg += (
                "\nThe env vars for these services are already in your env_overrides above. "
                "Focus on building and running the APPLICATION container only.\n"
            )

        past_skill = self._load_build_skill()
        if past_skill:
            user_msg += (
                "\n## Past build skill (from a previous successful build of this stack)\n\n"
                f"{past_skill}\n\n"
                "Use this as a starting point. Adapt as needed for this specific project."
            )

        if self.retry_context:
            user_msg += "\n" + self.retry_context

        result = self._run_loop(system, user_msg, TOOLS, self._handle_tool)
        self._save_build_trajectory(result)
        return result

    def _load_build_skill(self) -> str | None:
        """Load a past successful build pattern for this stack."""
        b = self.repo_brief
        if not b:
            return None
        stack_key = f"{b.language or 'unknown'}_{b.framework or 'unknown'}".lower()
        skill_dir = Path.home() / ".raincurve" / "skills"
        skill_file = skill_dir / f"{stack_key}.md"
        if skill_file.exists():
            try:
                content = skill_file.read_text(encoding="utf-8", errors="replace")
                return content[:3000]
            except (PermissionError, OSError):
                pass
        return None

    def _save_build_trajectory(self, result: AgentResult) -> None:
        """Save build outcome as a skill for future runs (Hermes learning loop)."""
        if not result.output:
            return
        b = self.repo_brief
        if not b:
            return

        stack_key = f"{b.language or 'unknown'}_{b.framework or 'unknown'}".lower()
        skill_dir = Path.home() / ".raincurve" / "skills"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / f"{stack_key}.md"

        output = result.output
        lines = [
            f"# Build skill: {stack_key}",
            f"Success: {result.success}",
            f"Duration: {result.duration_s:.0f}s, Calls: {result.tool_call_count}",
        ]

        if result.success:
            lines.append(f"Port: {output.get('port', '?')}")
            if output.get("services"):
                lines.append("Services:")
                for svc in output["services"]:
                    lines.append(f"  - {svc.get('name', '?')}: {svc.get('image', '?')}")
            if output.get("modifications"):
                lines.append("Modifications:")
                for mod in output["modifications"]:
                    lines.append(f"  - {mod}")
            if output.get("env_vars_used"):
                critical_vars = {k: v for k, v in output["env_vars_used"].items()
                                 if any(s in k.upper() for s in ("DATABASE", "REDIS", "PORT", "URL", "SECRET"))}
                if critical_vars:
                    lines.append("Key env vars:")
                    for k, v in list(critical_vars.items())[:15]:
                        lines.append(f"  {k}={v}")
        else:
            lines.append(f"Failure: {result.failure_reason or 'unknown'}")

        try:
            skill_file.write_text("\n".join(lines), encoding="utf-8")
        except (PermissionError, OSError):
            pass

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            return self._handle_bash(args)
        elif name == "text_editor":
            return self._handle_text_editor(args)
        elif name == "web_fetch":
            return self._handle_web_fetch(args)
        elif name == "web_search":
            return self._handle_web_search(args)
        else:
            return f"Unknown tool: {name}"

    _BLOCKING_PATTERNS = [
        "docker logs -f", "docker logs --follow",
        "tail -f", "tail --follow",
        "watch ", "nodemon",
        "-f rc-", "--follow rc-",
    ]

    _DESTRUCTIVE_PATTERNS = [
        "drop database", "drop schema", "truncate ",
        "dropdb ", "dropdb\n",
    ]

    def _handle_bash(self, args: dict) -> str:
        cmd = args["command"]
        cwd = args.get("cwd", "")

        cmd_lower = cmd.lower()
        for pattern in self._DESTRUCTIVE_PATTERNS:
            if pattern in cmd_lower:
                return (
                    "BLOCKED: Destructive database commands are forbidden. "
                    "NEVER run DROP DATABASE, DROP SCHEMA, or TRUNCATE. "
                    "If the database has issues, restart the Postgres container "
                    "or let the app's migration system handle it on startup."
                )
        timeout = args.get("timeout_s", 120)

        for pattern in self._BLOCKING_PATTERNS:
            if pattern in cmd:
                return (
                    f"BLOCKED: '{pattern}' is a blocking command that will hang. "
                    f"Use non-follow alternatives instead: 'docker logs <name>' (without -f), "
                    f"'docker logs --tail 50 <name>', or poll with a timeout."
                )

        # Guard: don't recreate containers the infra agent already started
        container_guard = self._guard_docker_run(cmd)
        if container_guard:
            return container_guard

        # Guard: don't retry same broken Dockerfile
        build_guard = self._guard_docker_build(cmd)
        if build_guard:
            return build_guard

        if cwd:
            full_cwd = str(Path(self.project_dir) / cwd)
        else:
            full_cwd = self.project_dir

        self._log(f"$ {cmd}")
        result = _exec_bash(cmd, full_cwd, timeout)

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}" if output else result.stderr

        if not output:
            output = "(no output)"

        raw = f"exit_code={result.exit_code}\n{output}"

        # Post-process: if docker build failed, classify and track
        if "docker build" in cmd_lower and result.exit_code != 0:
            raw = self._diagnose_build_failure(cmd, raw)

        return raw

    def _guard_docker_run(self, cmd: str) -> str | None:
        """Prevent recreating containers that already exist and are healthy."""
        import re as _re
        match = _re.search(r"docker\s+run\s+.*--name\s+(\S+)", cmd)
        if not match:
            return None
        name = match.group(1)

        check = _exec_bash(
            f'docker ps --filter "name=^{name}$" --format "{{{{.Status}}}}"',
            self.project_dir, 5,
        )
        if check.ok and check.stdout.strip() and "Up" in check.stdout:
            return (
                f"SKIPPED: Container '{name}' is already running. "
                f"Status: {check.stdout.strip()}. "
                f"Use it as-is — do NOT recreate infrastructure containers."
            )
        return None

    def _guard_docker_build(self, cmd: str) -> str | None:
        """Block repeated builds of the same broken Dockerfile."""
        if "docker build" not in cmd.lower():
            return None

        dockerfile_path = Path(self.project_dir) / "Dockerfile"
        if not dockerfile_path.exists():
            return None

        import hashlib
        content = dockerfile_path.read_text(encoding="utf-8", errors="replace")
        h = hashlib.md5(content.encode()).hexdigest()[:12]

        fails = self._build_failures.get(h, 0)
        if fails >= 2:
            return (
                f"BLOCKED: This exact Dockerfile has failed {fails} times. "
                f"You MUST edit the Dockerfile before trying again. "
                f"Last error:\n{self._last_build_error[:1000]}"
            )
        return None

    def _diagnose_build_failure(self, cmd: str, raw_output: str) -> str:
        """Classify a docker build failure and track it."""
        dockerfile_path = Path(self.project_dir) / "Dockerfile"
        if dockerfile_path.exists():
            import hashlib
            content = dockerfile_path.read_text(encoding="utf-8", errors="replace")
            h = hashlib.md5(content.encode()).hexdigest()[:12]
            self._build_failures[h] = self._build_failures.get(h, 0) + 1

        error_tail = raw_output[-2000:]
        self._last_build_error = error_tail

        diagnosis = "\n\n--- BUILD FAILURE DIAGNOSIS ---\n"
        lower = error_tail.lower()
        if "no such file or directory" in lower:
            diagnosis += "CAUSE: Missing file. Check paths in COPY/ADD commands.\n"
        elif "not found" in lower and ("npm" in lower or "pnpm" in lower or "yarn" in lower):
            diagnosis += "CAUSE: Package manager not found. Check base image has it installed.\n"
        elif "enoent" in lower or "module not found" in lower:
            diagnosis += "CAUSE: Missing dependency. Check package.json and install command.\n"
        elif "permission denied" in lower:
            diagnosis += "CAUSE: Permission issue. Check file ownership and USER directive.\n"
        elif "no space left" in lower:
            diagnosis += "CAUSE: Disk full. Run `docker system prune -f` first.\n"
        elif "syntax error" in lower or "unexpected token" in lower:
            diagnosis += "CAUSE: Syntax error in Dockerfile or build script.\n"
        elif "failed to solve" in lower or "failed to compute" in lower:
            diagnosis += "CAUSE: BuildKit resolver error. Check FROM image and COPY sources.\n"
        else:
            diagnosis += "CAUSE: Unknown. Read the error output above carefully.\n"

        diagnosis += (
            "ACTION REQUIRED: Read the error, edit the Dockerfile to fix it, "
            "then retry. Do NOT retry with the same Dockerfile."
        )
        return raw_output + diagnosis

    def _handle_text_editor(self, args: dict) -> str:
        command = args["command"]
        rel_path = args["path"]
        full_path = Path(self.project_dir) / rel_path

        if command == "view":
            return self._editor_view(full_path, args.get("view_range"))
        elif command == "create":
            return self._editor_create(full_path, args.get("file_text", ""))
        elif command == "str_replace":
            return self._editor_replace(full_path, args.get("old_str", ""), args.get("new_str", ""))
        else:
            return f"Unknown editor command: {command}"

    def _editor_view(self, path: Path, view_range: list[int] | None) -> str:
        if not path.exists():
            return f"File not found: {path}"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError) as e:
            return f"Error reading {path}: {e}"

        lines = content.splitlines()
        if view_range and len(view_range) == 2:
            start, end = view_range
            lines = lines[max(0, start - 1):end]
            numbered = [f"{i + start}: {line}" for i, line in enumerate(lines)]
        else:
            numbered = [f"{i + 1}: {line}" for i, line in enumerate(lines)]

        return "\n".join(numbered)

    def _editor_create(self, path: Path, content: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._log(f"Created {path.relative_to(self.project_dir)}")

        lint_err = self._lint_file(path)
        if lint_err:
            return f"File created but lint failed:\n{lint_err}"
        return f"Created {path}"

    def _editor_replace(self, path: Path, old_str: str, new_str: str) -> str:
        if not path.exists():
            return f"File not found: {path}"
        content = path.read_text(encoding="utf-8", errors="replace")
        if old_str not in content:
            return f"old_str not found in {path}"

        count = content.count(old_str)
        new_content = content.replace(old_str, new_str, 1)
        path.write_text(new_content, encoding="utf-8")
        self._log(f"Edited {path.relative_to(self.project_dir)}")

        lint_err = self._lint_file(path)
        if lint_err:
            path.write_text(content, encoding="utf-8")
            return f"Edit reverted — lint failed:\n{lint_err}"
        return f"Replaced in {path} ({count} occurrence(s) found, replaced first)"

    def _lint_file(self, path: Path) -> str | None:
        suffix = path.suffix
        if suffix == ".sh":
            r = _exec_bash(f"bash -n {path}", str(path.parent), timeout_s=10)
            if not r.ok:
                return r.stderr
        elif suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as e:
                return str(e)
        return None

    def _handle_web_fetch(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: no URL provided"

        self._log(f"Fetching: {url}")

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "raincurve/0.1 (sandbox-builder)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read(200_000)  # Cap at 200KB
                text = raw.decode("utf-8", errors="replace")

            # For HTML pages, extract text content (strip tags)
            if "html" in content_type:
                text = self._strip_html(text)

            return _truncate(text, head=8000, tail=4000)

        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            return f"Fetch failed: {e}"

    def _handle_web_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return "Error: no query provided"

        self._log(f"Searching: {query}")

        # Use DuckDuckGo HTML search (no API key needed)
        encoded_q = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_q}"

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "raincurve/0.1 (sandbox-builder)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read(100_000)
                html = raw.decode("utf-8", errors="replace")

            # Extract search result links and snippets
            results = self._parse_ddg_results(html)
            if not results:
                return "No results found."

            output_parts = []
            for i, r in enumerate(results[:8], 1):
                output_parts.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")

            return "\n\n".join(output_parts)

        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            return f"Search failed: {e}"

    @staticmethod
    def _strip_html(html: str) -> str:
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
        html = re.sub(r"<[^>]+>", " ", html)
        html = re.sub(r"\s+", " ", html)
        return html.strip()

    @staticmethod
    def _parse_ddg_results(html: str) -> list[dict[str, str]]:
        results = []
        # DuckDuckGo HTML results are in <a class="result__a"> with <a class="result__snippet">
        link_pattern = re.compile(
            r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL
        )
        snippet_pattern = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
        )

        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (url, title) in enumerate(links):
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            # DuckDuckGo wraps URLs in a redirect — extract the real URL
            if "uddg=" in url:
                match = re.search(r"uddg=([^&]+)", url)
                if match:
                    url = urllib.parse.unquote(match.group(1))
            results.append({"url": url, "title": title_clean, "snippet": snippet})

        return results

    def _verify_done(self, done_output: dict) -> str | None:
        import time

        port = done_output.get("port")
        health_path = done_output.get("health_path", "/")
        if not port:
            return "No port provided in done call."

        # 1. Check verification evidence is present
        verification = done_output.get("verification")
        if not verification:
            return (
                "MISSING VERIFICATION. You must run these checks before calling done:\n"
                "1. Query the database to confirm tables exist\n"
                "2. Curl each mock service to confirm it responds\n"
                "3. Run at least one end-to-end API test\n"
                "Include results in the 'verification' field of your done call."
            )

        failures = []

        # 2. Validate DB check
        db_check = verification.get("db_check", {})
        if not db_check.get("success"):
            failures.append(
                "DATABASE CHECK FAILED: No tables found or check was not run. "
                "Run migrations and verify the schema is loaded."
            )
        elif not db_check.get("tables_found"):
            failures.append(
                "DATABASE CHECK INCOMPLETE: 'tables_found' is empty. "
                "Query the database and list the tables."
            )

        # 3. Mock checks removed — infra agent handles services.
        # Build agent only needs to verify the app itself is running.

        if failures:
            # Gather container logs for diagnostics
            diag = []
            for svc in done_output.get("services", []):
                cname = svc.get("container_name", "")
                if cname:
                    r = _exec_bash(f"docker logs --tail 30 {cname}", self.project_dir, timeout_s=10)
                    if r.stdout or r.stderr:
                        diag.append(f"\n--- logs {cname} ---\n{r.stdout}\n{r.stderr}")

            return (
                "VERIFICATION FAILED. Fix these issues and try again:\n\n"
                + "\n\n".join(failures)
                + ("\n\n--- Container diagnostics ---" + "".join(diag) if diag else "")
            )

        # 5. Independent health check (we still verify the app responds)
        url = f"http://127.0.0.1:{port}{health_path}"
        self._log(f"Verifying health: {url}")
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status < 500:
                        self._log(f"Health check passed (HTTP {resp.status})")
                        return None
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            if attempt < 2:
                time.sleep(5)

        # Health check failed - gather diagnostics
        diag_parts = [f"App health check failed: {url} did not respond after 3 attempts."]
        for svc in done_output.get("services", []):
            cname = svc.get("container_name", "")
            if cname:
                r = _exec_bash(f"docker logs --tail 50 {cname}", self.project_dir, timeout_s=10)
                diag_parts.append(f"\n--- docker logs {cname} ---\n{r.stdout}\n{r.stderr}")
        r = _exec_bash("docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'", self.project_dir, timeout_s=10)
        diag_parts.append(f"\n--- docker ps ---\n{r.stdout}")
        return "\n".join(diag_parts)
