from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash

MAX_TOOL_CALLS = 20
MAX_WALLCLOCK_S = 120

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command to inspect files, test fixes, or check Docker state.",
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
        "name": "text_editor",
        "description": "View or edit files. Commands: view, str_replace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["view", "str_replace"]},
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
                "view_range": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["command", "path"],
        },
    },
    {
        "name": "done",
        "description": "Call when you've fixed the issue. Provide confidence 0-1.",
        "input_schema": {
            "type": "object",
            "properties": {
                "confidence": {"type": "number"},
                "fix_description": {"type": "string"},
            },
            "required": ["confidence", "fix_description"],
        },
    },
]

SYSTEM_PROMPT = """\
You are a Docker build error fixer. A docker build failed with these logs:

```
{error_logs}
```

The Dockerfile is at: {dockerfile_path}
The project directory is: {project_dir}

Your job:
1. Read the Dockerfile and any relevant project files.
2. Identify the root cause (missing system dep? wrong base image? wrong package name? \
network timeout? missing file?).
3. Apply the MINIMAL patch that fixes it.
4. Call `done` with your confidence level.

Common fixes:
- Missing apt deps → add `apt-get install` before the failing step
- pip install fails → add `--no-cache-dir`, check package name spelling
- Node version mismatch → bump `FROM node:XX-alpine`
- COPY file not found → adjust path relative to build context
- Permission denied → add `chmod` or adjust `USER` directive

Do NOT rewrite the Dockerfile. Minimal surgical changes only.
"""


class RecoveryAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S
    MODEL_OVERRIDE = None

    def __init__(
        self,
        project_dir: str,
        dockerfile_path: str,
        error_logs: str,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.dockerfile_path = dockerfile_path
        self.error_logs = error_logs

    def run(self) -> AgentResult:
        system = SYSTEM_PROMPT.format(
            error_logs=self.error_logs[:8000],
            dockerfile_path=self.dockerfile_path,
            project_dir=self.project_dir,
        )
        return self._run_loop(
            system,
            "Fix the Docker build error described above.",
            TOOLS,
            self._handle_tool,
        )

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            cmd = args["command"]
            timeout = args.get("timeout_s", 30)
            self._log(f"$ {cmd}")
            r = _exec_bash(cmd, self.project_dir, timeout)
            out = r.stdout + (f"\nSTDERR:\n{r.stderr}" if r.stderr else "")
            return f"exit_code={r.exit_code}\n{out or '(no output)'}"

        elif name == "text_editor":
            return self._handle_editor(args)

        return f"Unknown tool: {name}"

    def _handle_editor(self, args: dict) -> str:
        command = args["command"]
        rel_path = args["path"]
        full_path = Path(self.project_dir) / rel_path

        if command == "view":
            if not full_path.exists():
                return f"File not found: {full_path}"
            content = full_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            view_range = args.get("view_range")
            if view_range and len(view_range) == 2:
                lines = lines[max(0, view_range[0] - 1):view_range[1]]
                return "\n".join(f"{i + view_range[0]}: {l}" for i, l in enumerate(lines))
            return "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines))

        elif command == "str_replace":
            if not full_path.exists():
                return f"File not found: {full_path}"
            content = full_path.read_text(encoding="utf-8", errors="replace")
            old = args.get("old_str", "")
            new = args.get("new_str", "")
            if old not in content:
                return f"old_str not found in {full_path}"
            full_path.write_text(content.replace(old, new, 1), encoding="utf-8")
            self._log(f"Patched {rel_path}")
            return f"Replaced in {full_path}"

        return f"Unknown editor command: {command}"
