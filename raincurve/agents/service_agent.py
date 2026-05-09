from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash, _truncate


MAX_TOOL_CALLS = 60
MAX_WALLCLOCK_S = 600  # 10 minutes per service


TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. Use for: docker exec, curl, inspecting files inside"
            " containers, testing mock endpoints, verifying connectivity."
            " Returns stdout, stderr, exit_code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "timeout_s": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default 30.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "text_editor",
        "description": (
            "View or edit files in the project directory. Commands: "
            "view (read a file), create (write a new file), str_replace (replace text)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace"],
                },
                "path": {
                    "type": "string",
                    "description": "File path relative to project root",
                },
                "file_text": {
                    "type": "string",
                    "description": "For create: full file content",
                },
                "old_str": {
                    "type": "string",
                    "description": "For str_replace: exact text to find",
                },
                "new_str": {
                    "type": "string",
                    "description": "For str_replace: replacement text",
                },
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
            "Fetch a URL to learn about mock services, Docker images, SDK configuration."
        ),
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
        "description": (
            "Search the web for mock service setup, SDK patching techniques, Docker images."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "done",
        "description": (
            "Call when the mock service is running, the app code is patched, and you have "
            "verified the mock actually responds through the app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "mock_container": {
                    "type": "string",
                    "description": "Container name of the mock",
                },
                "mock_image": {
                    "type": "string",
                    "description": "Docker image used",
                },
                "verification": {
                    "type": "object",
                    "properties": {
                        "mock_url_tested": {"type": "string"},
                        "mock_response_status": {"type": "integer"},
                        "app_integration_tested": {
                            "type": "boolean",
                            "description": "Did you test the mock THROUGH the app?",
                        },
                        "test_description": {
                            "type": "string",
                            "description": "What you tested and what happened",
                        },
                    },
                    "required": [
                        "mock_url_tested",
                        "mock_response_status",
                        "app_integration_tested",
                        "test_description",
                    ],
                },
                "code_modifications": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files modified inside the container",
                },
                "env_vars_set": {
                    "type": "object",
                    "description": "Environment variables configured",
                },
            },
            "required": ["service_name", "mock_container", "verification"],
        },
    },
]


SYSTEM_PROMPT = """\
You are a service integration agent. Your ONLY job is to make ONE external service \
work locally by starting a real mock and patching the application code.

## Your service: {service_name}

## CRITICAL: All code is INSIDE the container

The app runs inside Docker container `{app_container}`. ALL code reading and \
patching happens via `docker exec`. NEVER read files from the host filesystem — \
you'll hit permission errors on Windows and the host source may differ from \
what's running.

## Step 1: LEARN THE CODEBASE STRUCTURE (do this FIRST — 2 commands max)

Before searching for anything, understand how the app is laid out:
```
docker exec {app_container} sh -c "ls /app/"
docker exec {app_container} sh -c "find /app -maxdepth 3 -type d | head -40"
```
This tells you: is it a monorepo? Where is the server code? Is it compiled JS \
in a `dist/` folder or source TS? This saves you from wasting 20 tool calls \
doing blind greps across thousands of files.

## Step 2: CHECK IF SERVICE IS ACTUALLY USED (1-2 commands max)

Do a TARGETED search — don't grep all of `/app`:
```
docker exec {app_container} sh -c "find /app -maxdepth 5 -name '*.js' -path '*dist*' | xargs grep -l '{service_name}' 2>/dev/null | head -10"
```
If zero results: this is a false positive. Call done immediately with \
`test_description: "Service not found in compiled code — false positive detection"`.

If results found: read the 1-2 most relevant files to understand the SDK usage.

Hint — files that import this SDK on the host (paths may differ inside container):
{import_files}

## Step 3: CHECK IF ALREADY RUNNING

```
docker exec {app_container} sh -c "env | grep -i {service_name}"
docker ps --filter "name=rc-" --format "{{{{.Names}}}} {{{{.Status}}}}"
```
If the service is already provided by compose (e.g., Redis), just verify it \
works and call done. Don't start a duplicate.

## Step 4: READ THE SDK INITIALIZATION CODE

Now that you know WHERE the code is, read the specific file:
```
docker exec {app_container} cat /app/path/to/the-file-you-found.js
```
Understand:
- How is the client constructed? (`new Stripe(key)`, `new S3Client(config)`)
- Does it read env vars for the endpoint/base URL?
- Can you override via env var or do you need to patch the code?

## Step 5: START THE MOCK + LEARN ABOUT IT (in parallel with step 4)
{mock_info}

{mock_start_cmd}

Check if already exists: `docker ps -a --filter "name={mock_container_name}"`
If running, skip. If stopped, `docker rm -f` and recreate.

## Step 6: PATCH — env vars first, code second

**Try env vars first** — restart the app container with the mock's env vars:
```
docker exec {app_container} sh -c "env | grep -i RELEVANT_VAR"
```
If the SDK reads its endpoint from an env var, you may just need to set it.

**If env vars aren't enough, patch the compiled code:**
```
docker exec {app_container} sh -c "sed -i 's|https://api.stripe.com|http://{mock_container_name}:12111|g' /app/path/to/file.js"
docker restart {app_container}
```
Patch the COMPILED `.js` files (in dist/build dirs), not `.ts` source files.

## Step 7: VERIFY END-TO-END

1. Curl the mock from the app container:
   `docker exec {app_container} sh -c "curl -sf http://{mock_container_name}:PORT/"`
2. Test through the app — make an API call that triggers this service
3. If it fails, check `docker logs {app_container} --tail 20` and fix

## Container info
- App container: `{app_container}`
- Docker network: `{network_name}`
- Project directory (host): `{project_dir}`

## Rules
- ALL file operations via `docker exec`. NEVER touch host files.
- Learn the codebase structure FIRST (step 1). Don't skip this.
- If the service isn't used (zero grep hits), call done IMMEDIATELY. Don't waste time.
- If already provided by compose, verify and call done. Don't duplicate.
- Be surgical — read specific files, not `grep -r` across entire /app.
- If something fails, read the error, fix it, try again.
"""


class ServiceAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S

    def __init__(
        self,
        project_dir: str,
        service_name: str,
        app_container: str,
        network_name: str,
        mock_container_name: str,
        import_files: list[str],
        mock_info: str,
        mock_start_cmd: str,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.service_name = service_name
        self.app_container = app_container
        self.network_name = network_name
        self.mock_container_name = mock_container_name
        self.import_files = import_files
        self.mock_info = mock_info
        self.mock_start_cmd = mock_start_cmd

    def run(self) -> AgentResult:
        import_files_str = (
            "\n".join(f"- `{f}`" for f in self.import_files)
            if self.import_files
            else "No import locations detected — search the codebase."
        )

        system = SYSTEM_PROMPT.format(
            service_name=self.service_name,
            import_files=import_files_str,
            mock_info=self.mock_info,
            mock_start_cmd=self.mock_start_cmd,
            app_container=self.app_container,
            mock_container_name=self.mock_container_name,
            network_name=self.network_name,
            project_dir=self.project_dir,
        )

        user_msg = (
            f"Set up a working local mock for {self.service_name}. "
            f"Read the code first to understand how the SDK is used, "
            f"start the mock container, patch the code inside {self.app_container}, "
            f"and verify it actually works end-to-end."
        )

        return self._run_loop(system, user_msg, TOOLS, self._handle_tool)

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

        elif name == "web_fetch":
            return self._handle_web_fetch(args)

        elif name == "web_search":
            return self._handle_web_search(args)

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
                r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            snippet_pattern = re.compile(
                r'class="result__snippet"[^>]*>(.*?)</a>',
                re.DOTALL,
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
            return "\n\n".join(results) if results else "No results found."
        except Exception as e:
            return f"Search failed: {e}"

    def _verify_done(self, done_output: dict) -> str | None:
        verification = done_output.get("verification", {})
        if not verification.get("app_integration_tested"):
            return (
                "You must test the mock THROUGH the app, not just curl the mock directly. "
                "Make an API call to the app that triggers it to use this service, "
                "and verify the response."
            )
        if not verification.get("test_description"):
            return "Provide a description of what you tested and what happened."
        return None
