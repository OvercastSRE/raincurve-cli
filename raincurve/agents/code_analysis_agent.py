from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent

MAX_TOOL_CALLS = 80
MAX_WALLCLOCK_S = 300

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".raincurve", "vendor", ".yarn", ".pnp", "coverage", ".turbo",
}
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".java", ".rs",
    ".yml", ".yaml", ".toml", ".json", ".env", ".cfg", ".ini",
}

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a source file from the project. Returns content with line numbers. "
            "Use to trace SDK initialization, read config files, understand auth flows, "
            "and find route definitions. Max 500 lines per call — use start_line/end_line "
            "for large files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to project root",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Start line (1-indexed). Default: 1.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "End line. Default: start_line + 499.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": (
            "Search for a regex pattern across project source files. Returns file paths "
            "with matching lines and line numbers. Use to find: SDK imports, env var reads "
            "(process.env, os.environ, os.getenv), route definitions (@app.route, router.get), "
            "auth middleware, database connections, and specific function calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "file_glob": {
                    "type": "string",
                    "description": (
                        "File extension filter. E.g. '*.ts', '*.py', '*.js'. "
                        "Default: all source files."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max matches to return. Default: 20.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List files and directories at a given path. Use to understand project "
            "structure, find source directories, and locate config files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path relative to project root. Default: root.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max directory depth to show. Default: 2.",
                },
            },
        },
    },
    {
        "name": "done",
        "description": (
            "Submit the complete code analysis. Call when you have mapped all services, "
            "env vars, auth flow, routes, database, and domain understanding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "project_dir": {"type": "string"},
                "language": {"type": "string"},
                "framework": {"type": "string"},
                "package_manager": {"type": "string"},
                "sdk_usages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "service_name": {"type": "string"},
                            "sdk_package": {"type": "string"},
                            "init_file": {"type": "string"},
                            "init_code_snippet": {"type": "string"},
                            "base_url_env_var": {"type": "string"},
                            "base_url_hardcoded": {"type": "string"},
                            "api_key_env_var": {"type": "string"},
                            "endpoints_called": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "patching_strategy": {"type": "string"},
                            "files_using_sdk": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["service_name", "sdk_package", "init_file"],
                    },
                },
                "env_vars": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "source_files": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "default_value": {"type": "string"},
                            "purpose": {"type": "string"},
                            "required": {"type": "boolean"},
                        },
                        "required": ["name"],
                    },
                },
                "auth_flow": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string"},
                        "middleware_file": {"type": "string"},
                        "login_endpoint": {"type": "string"},
                        "login_body_schema": {"type": "object"},
                        "token_type": {"type": "string"},
                        "default_credentials": {"type": "object"},
                        "bypass_strategy": {"type": "string"},
                    },
                },
                "api_routes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "method": {"type": "string"},
                            "path": {"type": "string"},
                            "handler_file": {"type": "string"},
                            "requires_auth": {"type": "boolean"},
                            "description": {"type": "string"},
                        },
                        "required": ["method", "path"],
                    },
                },
                "database": {
                    "type": "object",
                    "properties": {
                        "db_type": {"type": "string"},
                        "connection_env_var": {"type": "string"},
                        "connection_file": {"type": "string"},
                        "migration_tool": {"type": "string"},
                        "migration_command": {"type": "string"},
                        "seed_command": {"type": "string"},
                        "schema_summary": {"type": "string"},
                        "business_domain": {"type": "string"},
                    },
                    "required": ["db_type"],
                },
                "entry_point": {"type": "string"},
                "start_command": {"type": "string"},
                "build_command": {"type": "string"},
                "dockerfile_path": {"type": "string"},
                "compose_path": {"type": "string"},
                "app_description": {"type": "string"},
                "core_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "core_user_flows": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "project_name", "project_dir", "language",
                "sdk_usages", "env_vars", "app_description",
            ],
        },
    },
]

SYSTEM_PROMPT = """\
You are a code analysis agent. Your job is to read a codebase and produce \
a structured understanding of it. This understanding will be used by downstream \
agents to build a production sandbox replica — so accuracy is critical.

You are NOT building anything. You are only reading and analyzing code.

## CRITICAL: Start with project documentation

The project docs below are your MOST IMPORTANT source of truth. The README \
typically describes: what the app does, how to set it up, build/start commands, \
environment variables, database requirements, and external dependencies. Read \
and extract information from the docs BEFORE scanning source files. Only scan \
source files for details the docs don't cover.

## Pre-Analysis (automated scan — may have false positives)

- Language: {language}
- Framework: {framework}
- Package manager: {package_manager}
- Detected services: {detected_services}
- Import locations (approximate): {import_locations}
- Database: {database_type}
- Dockerfile: {dockerfile_path}
- Compose: {compose_path}
- Migration tool: {migration_tool}, command: {migration_command}
- Start command: {start_command}
- Build command: {build_command}

## Project Documentation

{project_docs}

## Your Task

### 1. Extract everything you can from the project docs above
- App description, core entities, user flows
- Build/start commands, environment variables, database setup
- External service dependencies
- Auth flow and default credentials

### 2. Understand the project structure
- Read the entry point file to understand the app's architecture
- Identify the main source directories

### 3. Find ALL external services — not just the detected ones
The pre-analysis detected some services via regex, but it MISSES many. You must \
independently discover ALL external services by:
a. Searching for client libraries (e.g., @clickhouse/client, @aws-sdk/*, stripe, \
   twilio, @sendgrid/mail, ioredis, kafkajs, etc.)
b. Looking for any file that creates a connection to an external service
c. Checking env vars that reference external URLs (DATABASE_URL, REDIS_URL, \
   CLICKHOUSE_URL, KAFKA_BROKER, etc.)

For EACH service you find (detected or discovered):
a. Find where the SDK client is initialized (the constructor call)
b. Read that file to capture the exact initialization code (3-10 lines)
c. Determine: does it read a base URL from an env var? Is the URL hardcoded?
d. Recommend a patching strategy: "set ENV_VAR env var" or "patch init code in FILE"
e. If a detected service has ZERO results, mark it as false positive

### 4. Map environment variable usage
- Search for `process.env` (Node), `os.environ` / `os.getenv` (Python), etc.
- For each env var: which files read it, what's it used for, is there a default?
- Pay special attention to env vars for database URLs, API keys, and secrets

### 5. Identify the auth flow
- Search for auth middleware, login routes, JWT/session handling
- Find the login endpoint and its expected body shape
- Look for default/seed credentials in seed files, migration files, or constants

### 6. Map API routes (top 10-15 most important)

### 7. Database understanding
- Find the database connection configuration
- Identify the ORM and migration tool
- Summarize the schema (main tables/collections)

## Approach

1. START by reading the project docs above — extract as much as possible
2. Use `list_directory` to understand the source layout (1-2 calls max)
3. For each detected service, use `search_files` to find imports, then `read_file`
4. Use `search_files` for env var patterns, auth middleware, route definitions
5. Synthesize everything into the `done` call
6. Be FAST — the build agent is waiting for you. Prioritize breadth over depth.

## Quality Bar

Before calling done, verify:
- Every detected service has been investigated (confirmed or marked false positive)
- At least 5 env vars are mapped
- Auth flow is identified (or confirmed absent)
- Database type and connection are identified
- App description and core entities are filled in

## Important

- You have {project_dir} as the project root. All paths are relative to it.
- Do NOT modify any files. This is read-only analysis.
- Be efficient with tool calls. Each search should be targeted, not broad.
- If a file is very large (>500 lines), read specific sections using start_line/end_line.
"""


class CodeAnalysisAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S

    def __init__(
        self,
        project_dir: str,
        project_name: str,
        repo_brief: Any | None = None,
        detection_result: Any | None = None,
        project_docs: Any | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.project_name = project_name
        self.repo_brief = repo_brief
        self.detection_result = detection_result
        self.project_docs = project_docs

    def run(self) -> AgentResult:
        b = self.repo_brief
        d = self.detection_result

        detected_services = ""
        import_locations = ""
        if d and d.detected_services:
            detected_services = ", ".join(s.name for s in d.detected_services)
            if d.import_hits:
                lines = []
                for svc, files in d.import_hits.items():
                    lines.append(f"  {svc}: {', '.join(files[:5])}")
                import_locations = "\n".join(lines)

        docs_content = ""
        if self.project_docs:
            for attr in ["claude_md", "cursor_rules", "agents_md", "readme", "contributing"]:
                val = getattr(self.project_docs, attr, None)
                if val:
                    label = attr.replace("_", " ").title()
                    docs_content += f"\n### {label}\n{val[:5000]}\n"
            for name, content in (self.project_docs.custom_docs or {}).items():
                docs_content += f"\n### {name}\n{content[:3000]}\n"

        if not docs_content:
            docs_content = "(no project documentation found)"

        system = SYSTEM_PROMPT.format(
            language=b.language if b else "unknown",
            framework=b.framework or "unknown" if b else "unknown",
            package_manager=b.package_manager or "unknown" if b else "unknown",
            detected_services=detected_services or "none detected",
            import_locations=import_locations or "  (none)",
            database_type=b.database_type or "unknown" if b else "unknown",
            dockerfile_path=b.dockerfile_path or "none" if b else "none",
            compose_path=b.compose_path or "none" if b else "none",
            migration_tool=b.migration_tool or "unknown" if b else "unknown",
            migration_command=b.migration_command or "unknown" if b else "unknown",
            start_command=b.start_command or "unknown" if b else "unknown",
            build_command=b.build_command or "unknown" if b else "unknown",
            project_docs=docs_content,
            project_dir=self.project_dir,
        )

        user_msg = (
            f"Analyze the codebase at {self.project_dir}. "
            f"Project name: {self.project_name}. "
            f"Produce a complete CodeContext with service map, env vars, auth flow, "
            f"routes, database info, and domain understanding."
        )

        return self._run_loop(system, user_msg, TOOLS, self._handle_tool)

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "read_file":
            return self._handle_read_file(args)
        elif name == "search_files":
            return self._handle_search_files(args)
        elif name == "list_directory":
            return self._handle_list_directory(args)
        return f"Unknown tool: {name}"

    def _handle_read_file(self, args: dict) -> str:
        rel_path = args.get("path", "")
        if not rel_path:
            return "Error: 'path' is required."

        full_path = Path(self.project_dir) / rel_path
        if not full_path.exists():
            return f"File not found: {rel_path}"
        if not full_path.is_file():
            return f"Not a file: {rel_path}"

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError) as e:
            return f"Error reading {rel_path}: {e}"

        lines = content.splitlines()
        start = max(0, args.get("start_line", 1) - 1)
        end = args.get("end_line", start + 500)
        end = min(end, len(lines))

        selected = lines[start:end]
        numbered = [f"{i + start + 1}: {line}" for i, line in enumerate(selected)]

        result = "\n".join(numbered)
        if end < len(lines):
            result += f"\n\n[... {len(lines) - end} more lines. Use start_line={end + 1} to continue.]"

        self._log(f"Read {rel_path} (lines {start + 1}-{end} of {len(lines)})")
        return result

    def _handle_search_files(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        if not pattern:
            return "Error: 'pattern' is required."

        file_glob = args.get("file_glob", "")
        max_results = args.get("max_results", 20)

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Invalid regex: {e}"

        root = Path(self.project_dir)
        matches: list[str] = []

        for fpath in root.rglob("*"):
            if any(skip in fpath.parts for skip in SKIP_DIRS):
                continue
            try:
                if not fpath.is_file():
                    continue
            except OSError:
                continue
            if file_glob and not fpath.match(file_glob):
                continue
            if fpath.suffix not in SOURCE_EXTENSIONS:
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except (PermissionError, OSError):
                continue

            for i, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    rel = str(fpath.relative_to(root)).replace("\\", "/")
                    matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        if not matches:
            return f"No matches for pattern: {pattern}"

        self._log(f"Search '{pattern}' → {len(matches)} matches")
        return "\n".join(matches)

    def _handle_list_directory(self, args: dict) -> str:
        rel_path = args.get("path", "")
        max_depth = args.get("max_depth", 2)

        root = Path(self.project_dir) / rel_path if rel_path else Path(self.project_dir)
        if not root.exists():
            return f"Directory not found: {rel_path or '.'}"

        lines: list[str] = []
        self._walk_dir(root, root, 0, max_depth, lines)

        if not lines:
            return "(empty directory)"
        return "\n".join(lines[:200])

    def _walk_dir(
        self, path: Path, root: Path, depth: int, max_depth: int, lines: list[str],
    ) -> None:
        if depth > max_depth or len(lines) > 200:
            return

        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, OSError):
            return

        indent = "  " * depth
        for entry in entries:
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                self._walk_dir(entry, root, depth + 1, max_depth, lines)
            elif entry.is_file() and len(lines) < 200:
                lines.append(f"{indent}{entry.name}")

    def _verify_done(self, done_output: dict) -> str | None:
        issues = []

        sdk_usages = done_output.get("sdk_usages", [])
        env_vars = done_output.get("env_vars", [])
        app_desc = done_output.get("app_description", "")

        if not app_desc:
            issues.append("app_description is empty — describe what this app does.")

        if not env_vars:
            issues.append(
                "env_vars is empty — search for process.env / os.environ to find env vars."
            )

        if self.detection_result and self.detection_result.detected_services:
            detected_names = {s.name for s in self.detection_result.detected_services}
            analyzed_names = {s.get("service_name", "") for s in sdk_usages}
            missing = detected_names - analyzed_names
            if missing:
                issues.append(
                    f"These detected services were not analyzed: {', '.join(missing)}. "
                    f"Investigate each one or mark as false positive."
                )

        if issues:
            return "CodeContext incomplete:\n" + "\n".join(f"- {i}" for i in issues)
        return None
