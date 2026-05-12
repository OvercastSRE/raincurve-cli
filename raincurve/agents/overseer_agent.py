from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent
from raincurve.agents.environment_agent import EnvironmentAgent
from raincurve.agents.memory import ProceduralMemory


STRATEGIES = [
    "dockerfile_build",
    "bind_mount_dev",
    "nixpacks",
    "pull_image",
    "compose_up",
]

TOOLS = [
    {
        "name": "execute_build",
        "description": (
            "Delegate to the build agent with a specific strategy and directive. "
            "The build agent will attempt to bring up the application using your instructions. "
            "Returns the build result (success/failure with details). "
            "If it fails, reflect on why and try a different strategy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "enum": STRATEGIES,
                    "description": "Which build strategy to use",
                },
                "directive": {
                    "type": "string",
                    "description": (
                        "Free-form instructions for the build agent. Explain exactly how "
                        "to execute this strategy for THIS specific project. Be concrete — "
                        "include the base image, commands, port, etc."
                    ),
                },
            },
            "required": ["strategy", "directive"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the project to inform your strategy decision.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to project root (e.g. 'package.json', 'Dockerfile')",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "save_learning",
        "description": (
            "Save what you learned about building this type of project. "
            "This is persisted across projects with the same stack and loaded "
            "on future runs so you don't repeat mistakes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Markdown describing: which strategy worked, which failed and why, "
                        "and recommendations for this stack. Be specific and actionable."
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "done",
        "description": "Signal that the build is complete. Pass through the build result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "build_output": {
                    "type": "object",
                    "description": "The full output from the successful build agent",
                },
                "strategies_tried": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "strategy": {"type": "string"},
                            "success": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                    },
                    "description": "Summary of all strategies attempted",
                },
            },
            "required": ["success"],
        },
    },
]

SYSTEM_PROMPT = """\
You are the Build Overseer — a strategic agent that decides HOW to bring up \
an application as a running Docker sandbox.

## Your role

You do NOT build the app yourself. You choose a strategy, write a clear \
directive, and delegate to a build agent. If the build agent fails, you \
reflect on why, choose a different strategy, and try again.

## Available strategies

1. **dockerfile_build** — Write or use a Dockerfile to build an image, then run it. \
Best for: apps with existing Dockerfiles, complex builds, multi-service apps.

2. **bind_mount_dev** — Start a base runtime container (node:20, python:3.12-slim, etc.) \
with the project source bind-mounted as a volume. Install deps and run the dev server \
directly inside. Best for: simple apps deployed on Vercel/Netlify/Railway with no \
Dockerfile, standard frameworks (Next.js, SvelteKit, Nuxt, Django, FastAPI, Rails). \
This is often the fastest strategy — no image build needed.

3. **nixpacks** — Use nixpacks to auto-detect the stack and build a Docker image. \
Best for: when you're unsure about the build process. nixpacks handles most stacks \
automatically. Command: `docker run --rm -v "$PROJECT:/src" -v /var/run/docker.sock:/var/run/docker.sock ghcr.io/railwayapp/nixpacks:latest build /src --name IMAGE_NAME`

4. **pull_image** — Pull a pre-built image specified in docker-compose.yml. \
Best for: projects that publish official Docker images. Much faster than building.

5. **compose_up** — Run `docker compose up` using the project's existing compose file. \
Best for: projects with well-configured docker-compose.yml that includes all services.

## Decision framework

- No Dockerfile + standard framework (Next.js, SvelteKit, Nuxt, Remix, Django, \
FastAPI, Rails, Express) → **try bind_mount_dev first**
- Has Dockerfile → **try dockerfile_build first**
- Has docker-compose with `image:` (pre-built) → **try pull_image first**
- Has docker-compose with `build:` → **try compose_up or dockerfile_build**
- Unsure / exotic stack → **try nixpacks**
- IMPORTANT: Past learnings (if provided) override these defaults

## Key principles

- Read past learnings FIRST — they tell you what worked and what failed
- Inspect project files (package.json, Dockerfile, docker-compose.yml) before deciding
- When writing a directive, be SPECIFIC: include the exact base image, install command, \
start command, port, and any gotchas
- For bind_mount_dev, use the project's dev server command (e.g., `npm run dev`, \
`python manage.py runserver 0.0.0.0:8000`)
- NEVER try the same strategy with the same approach twice
- After success OR final failure, ALWAYS save_learning so future builds are faster
- The build agent has bash, text_editor, web_search, and web_fetch tools

## Container conventions

- Network: `{network_name}`
- Container name: `{container_name}`
- Label: `--label rc-aux-of={project_name}`
- Always use `--restart=unless-stopped`
- Do NOT set --memory or --cpus on the app container
- Infrastructure (postgres, redis, etc.) is already running — do NOT recreate it
"""


class OverseerAgent(BaseAgent):
    MAX_TOOL_CALLS = 30
    MAX_WALLCLOCK_S = 3600

    def __init__(
        self,
        project_dir: str,
        project_name: str,
        container_name: str,
        network_name: str,
        env_overrides: dict[str, str],
        detection_result: object,
        repo_brief: object,
        pre_started_services: set[str],
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.project_name = project_name
        self.container_name = container_name
        self.network_name = network_name
        self.env_overrides = env_overrides
        self.detection_result = detection_result
        self.repo_brief = repo_brief
        self.pre_started_services = pre_started_services
        self._build_attempts: list[dict] = []
        self._last_build_result: AgentResult | None = None
        self._proc_mem = ProceduralMemory()
        self._stack_key = self._compute_stack_key()

    def _compute_stack_key(self) -> str:
        b = self.repo_brief
        if b:
            lang = (b.language or "unknown").lower()
            fw = (b.framework or "unknown").lower()
            return f"{lang}_{fw}"
        return "unknown_unknown"

    def run(self) -> AgentResult:
        system = SYSTEM_PROMPT.format(
            project_name=self.project_name,
            container_name=self.container_name,
            network_name=self.network_name,
        )

        user_msg = self._build_initial_message()

        result = self._run_loop(system, user_msg, TOOLS, self._handle_tool)

        if result.success and result.output:
            build_output = result.output.get("build_output")
            if build_output:
                result.output = build_output

        return result

    def _build_initial_message(self) -> str:
        b = self.repo_brief
        parts = [f"Bring up the application at: {self.project_dir}\n"]

        if b:
            parts.append("## Project analysis\n")
            parts.append(f"- Language: {b.language}")
            if b.framework:
                parts.append(f"- Framework: {b.framework}")
            if b.package_manager:
                parts.append(f"- Package manager: {b.package_manager}")
            if b.has_dockerfile:
                parts.append(f"- Dockerfile: {b.dockerfile_path}")
            else:
                parts.append("- No Dockerfile")
            if b.has_compose:
                parts.append(f"- Docker Compose: {b.compose_path}")
                if b.compose_analysis:
                    for svc in b.compose_analysis.services[:5]:
                        parts.append(f"  - {svc.name}: image={svc.image or 'build'}")
            else:
                parts.append("- No docker-compose")
            if b.start_command:
                parts.append(f"- Start command: {b.start_command}")
            if b.build_command:
                parts.append(f"- Build command: {b.build_command}")
            if b.app_port:
                parts.append(f"- App port: {b.app_port}")
            if b.database_type:
                parts.append(f"- Database: {b.database_type}")

        if self.pre_started_services:
            parts.append("\n## Infrastructure already running\n")
            parts.append(f"These are UP: {', '.join(sorted(self.pre_started_services))}")
            parts.append("Do NOT recreate them.")

        if self.env_overrides:
            parts.append(f"\n## Environment variables ({len(self.env_overrides)} vars provided)")
            for k, v in list(self.env_overrides.items())[:20]:
                display = v if len(v) < 60 else v[:30] + "..."
                parts.append(f"  {k}={display}")

        learnings = self._proc_mem.load(self._stack_key)
        if learnings:
            parts.append(f"\n## Past learnings for {self._stack_key}\n")
            parts.append(learnings)
        else:
            parts.append(f"\nNo past learnings for {self._stack_key}. This is a first attempt.")

        parts.append(
            "\nInspect the project files if needed, then choose a strategy and execute_build."
        )
        return "\n".join(parts)

    def _handle_tool(self, name: str, args: dict) -> Any:
        if name == "execute_build":
            return self._tool_execute_build(args)
        elif name == "read_file":
            return self._tool_read_file(args)
        elif name == "save_learning":
            return self._tool_save_learning(args)
        elif name == "done":
            return args
        return f"Unknown tool: {name}"

    _STALL_TIMEOUT_S = 180

    def _tool_execute_build(self, args: dict) -> str:
        strategy = args.get("strategy", "dockerfile_build")
        directive = args.get("directive", "")

        attempt_num = len(self._build_attempts) + 1
        self._log(f"Strategy #{attempt_num}: {strategy}")

        strategy_prompt = (
            f"\n\n## BUILD STRATEGY (from Overseer — follow this)\n\n"
            f"Strategy: **{strategy}**\n\n"
            f"{directive}\n\n"
            f"Follow the strategy above. If it doesn't work, call done with "
            f"success=false and explain what went wrong — the overseer will "
            f"choose a different approach.\n"
        )

        last_activity = [time.time()]

        def tracking_log(msg: str) -> None:
            last_activity[0] = time.time()
            self.on_log(msg)

        agent = EnvironmentAgent(
            project_dir=self.project_dir,
            project_name=self.project_name,
            container_name=self.container_name,
            network_name=self.network_name,
            env_overrides=self.env_overrides,
            detection_result=self.detection_result,
            repo_brief=self.repo_brief,
            on_log=tracking_log,
            pre_started_services=self.pre_started_services,
            pipe_handled_services=set(),
            strategy_directive=strategy_prompt,
        )

        result_holder: list[AgentResult | None] = [None]

        def run_agent() -> None:
            result_holder[0] = agent.run()

        thread = threading.Thread(target=run_agent, daemon=True)
        thread.start()

        while thread.is_alive():
            thread.join(timeout=10)
            stall_s = time.time() - last_activity[0]
            if stall_s > self._STALL_TIMEOUT_S:
                self._log(
                    f"No progress for {int(stall_s)}s — aborting strategy '{strategy}'"
                )
                agent._abort_event.set()
                thread.join(timeout=30)
                break

        result = result_holder[0]
        if result is None:
            result = AgentResult(
                success=False,
                failure_reason=f"Aborted: no progress for {self._STALL_TIMEOUT_S}s",
            )
        self._last_build_result = result

        attempt = {
            "strategy": strategy,
            "success": result.success,
            "duration_s": result.duration_s,
            "tool_calls": result.tool_call_count,
            "reason": result.failure_reason or "",
        }
        self._build_attempts.append(attempt)

        if result.success:
            output = result.output or {}
            return (
                f"BUILD SUCCEEDED with strategy '{strategy}'\n"
                f"Duration: {result.duration_s:.0f}s, Tool calls: {result.tool_call_count}\n"
                f"Port: {output.get('port')}\n"
                f"Services: {json.dumps(output.get('services', []), indent=2)}\n"
                f"Health: {output.get('health_path')}\n\n"
                f"Now save_learning and call done with success=true and "
                f"build_output containing the full result."
            )
        else:
            return (
                f"BUILD FAILED with strategy '{strategy}'\n"
                f"Duration: {result.duration_s:.0f}s, Tool calls: {result.tool_call_count}\n"
                f"Reason: {result.failure_reason}\n\n"
                f"Reflect on why this failed and try a different strategy. "
                f"Strategies tried so far: {[a['strategy'] for a in self._build_attempts]}"
            )

    def _tool_read_file(self, args: dict) -> str:
        from pathlib import Path
        rel_path = args.get("path", "")
        full = Path(self.project_dir) / rel_path
        if not full.exists():
            return f"File not found: {rel_path}"
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
            if len(content) > 5000:
                content = content[:5000] + f"\n\n[... truncated, {len(content)} chars total]"
            return content
        except (PermissionError, OSError) as e:
            return f"Error reading {rel_path}: {e}"

    def _tool_save_learning(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return "No content to save"

        header = f"# Build learnings: {self._stack_key}\n\n"
        attempts_section = "## Attempts history\n\n"
        for a in self._build_attempts:
            status = "SUCCESS" if a["success"] else "FAILED"
            attempts_section += (
                f"- {a['strategy']}: {status} "
                f"({a['duration_s']:.0f}s, {a['tool_calls']} calls)"
            )
            if a["reason"]:
                attempts_section += f" — {a['reason'][:200]}"
            attempts_section += "\n"

        full_content = header + attempts_section + "\n" + content
        self._proc_mem.save(self._stack_key, full_content)
        self._log(f"Saved learnings for {self._stack_key}")
        return f"Learnings saved to ~/.raincurve/skills/{self._stack_key}.md"

    def _verify_done(self, done_output: dict) -> str | None:
        if done_output.get("success"):
            has_successful_build = any(a["success"] for a in self._build_attempts)
            if not has_successful_build:
                return (
                    "You claimed success but no build strategy actually succeeded. "
                    "Do not call done with success=true unless the app is verified running. "
                    "Either try another strategy or call done with success=false."
                )
        else:
            if len(self._build_attempts) < 3:
                return (
                    f"You've only tried {len(self._build_attempts)} strategies. "
                    f"There are {len(STRATEGIES)} available. Try a different one "
                    f"before giving up."
                )
        return None
