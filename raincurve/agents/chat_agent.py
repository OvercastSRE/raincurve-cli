from __future__ import annotations

import json
import subprocess
from typing import Any, Callable

from raincurve.agents.base_agent import AgentResult, BaseAgent, _exec_bash, _truncate

MAX_TOOL_CALLS = 60
MAX_WALLCLOCK_S = 600

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command against the local sandbox. Use for:\n"
            "- curl/httpx against the running app at http://localhost:{app_port}\n"
            "- docker exec into containers for DB queries, log inspection, etc.\n"
            "- Reading files in the project directory\n"
            "- Running scripts, installing tools, anything needed\n\n"
            "Each bash call is stateless — chain with && or write temp files for state.\n"
            "Output is capped at 8KB (head + tail with elision marker)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "browser",
        "description": (
            "Launch a browser scenario against the running app. Use this when the task "
            "requires VISUAL interaction — clicking buttons, filling forms, navigating "
            "pages, checking what a real user would see. The browser runs inside a Docker "
            "container on the same network as the app.\n\n"
            "Provide a natural language goal and the browser agent will execute it, "
            "taking screenshots and interacting with the page.\n\n"
            "Use bash for API-level testing. Use browser for UI-level testing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What the browser should do, e.g. 'Login and check the dashboard shows data'",
                },
                "login": {
                    "type": "boolean",
                    "description": "Whether to auto-login before starting. Default true.",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Maximum browser actions. Default 25.",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "reconfigure",
        "description": (
            "Modify the sandbox infrastructure. Use when the user wants to add/remove "
            "containers, change resource limits, add a load balancer, scale replicas, etc. "
            "This tool modifies .raincurve/config.json and executes Docker commands to "
            "apply the changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add_container", "remove_container", "scale_replicas", "add_load_balancer", "update_resources"],
                    "description": "What infrastructure change to make",
                },
                "service_name": {
                    "type": "string",
                    "description": "Name of the service to modify",
                },
                "params": {
                    "type": "object",
                    "description": "Parameters for the action (image, replicas, memory, cpus, etc.)",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "done",
        "description": (
            "Call when you've completed the user's request. Provide a verdict and summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["succeeded", "found_issue", "stuck", "errored"],
                },
                "summary": {
                    "type": "string",
                    "description": "1-4 sentence summary of what you did and found.",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                            "evidence": {"type": "string"},
                            "endpoint": {"type": "string"},
                        },
                    },
                    "description": "Structured findings if issues were discovered.",
                },
            },
            "required": ["verdict", "summary"],
        },
    },
]

SYSTEM_PROMPT = """\
You are an interactive assistant for a production sandbox. The user will ask you \
to do things against a live, running application — test flows, find bugs, query \
data, check behavior, run scenarios. You have full access to the sandbox environment.

## Your tools

- `bash`: Run shell commands locally. The app is at http://localhost:{app_port}. \
  Docker containers are on the `{network}` network. Use for API calls, DB queries, \
  log inspection, file reading, and anything that doesn't need a visual browser. \
  NEVER run blocking/long-running processes (http servers, watchers, `tail -f`, etc.) \
  directly — they will hang forever. Always run servers inside Docker containers \
  on the sandbox network instead of on the host.

- `browser`: Launch a visual browser scenario. Use when the task requires clicking \
  through the UI, filling forms, checking what a real user would see. A Playwright \
  container runs on the same Docker network and interacts with the app visually.

- `reconfigure`: Modify sandbox infrastructure. Use when the user wants to add \
  containers, scale replicas, add a load balancer, change resource limits. This \
  executes Docker commands to apply changes in real-time.

- `done`: Signal completion with your verdict and findings.

## Environment

- App: http://localhost:{app_port}
- Project directory: {project_dir}
- Docker network: {network}
- Containers: {containers}
- Login credentials: {login_info}
- Database: {db_info}

## Sandbox isolation (CRITICAL — read this first)

NEVER create files in the user's project directory. The project directory is READ-ONLY \
to you. All diagnostic scripts, test files, reports, and temporary data MUST go inside \
Docker containers. The user's repo should look exactly the same after you're done.

How to run scripts:
- Use `docker exec <container> ...` for quick commands (DB queries, curl, etc.)
- For multi-line scripts, use `docker exec -i <container> python3 -c "..."` for short ones.
- For longer scripts, `docker cp` a temp file into the container, run it, then clean up.
- For test HTML pages or static files, serve them from inside a container, not the host.

Preferred patterns:
- DB queries: `docker exec <db-container> <db-cli> ...`
- API calls: `curl http://localhost:{app_port}/api/...`
- Complex test scripts: `docker cp` into the app container and run there.

## Shell rules

1. Keep individual bash commands SHORT — under 2000 characters.
2. Avoid multi-line echo/heredoc chains. Use `docker exec` with short scripts instead.
3. For curl with auth tokens, keep them in simple `-H` flags.

## Guidelines

1. PREFER `bash` for speed. Most tasks don't need a browser — curl is faster.
2. Use `browser` when the user explicitly wants visual interaction, or when you \
   need to test client-side behavior (JS rendering, form validation, navigation flow).
3. Cache auth tokens. Login once via bash, reuse the token for subsequent API calls.
4. Use `docker exec` for ALL script execution — never run test scripts on the host.
5. If you find issues, report them as structured `findings` in your `done` call.
6. Be thorough but efficient. Don't over-test — focus on what the user asked for.

{repo_context}
"""


class ChatAgent(BaseAgent):
    MAX_TOOL_CALLS = MAX_TOOL_CALLS
    MAX_WALLCLOCK_S = MAX_WALLCLOCK_S

    def __init__(
        self,
        project_dir: str,
        app_port: int,
        network: str,
        containers: str,
        login_info: str,
        db_info: str,
        repo_context: str = "",
        on_log: Callable[[str], None] | None = None,
        on_browser: Callable[[dict], str] | None = None,
    ) -> None:
        super().__init__(project_dir, on_log)
        self.app_port = app_port
        self.network = network
        self.containers = containers
        self.login_info = login_info
        self.db_info = db_info
        self.repo_context = repo_context
        self.on_browser = on_browser
        self._messages: list[dict] = []

    def chat(self, user_message: str) -> AgentResult:
        system = SYSTEM_PROMPT.format(
            app_port=self.app_port,
            project_dir=self.project_dir,
            network=self.network,
            containers=self.containers,
            login_info=self.login_info,
            db_info=self.db_info,
            repo_context=self.repo_context,
        )

        self._messages.append({"role": "user", "content": user_message})
        return self._run_loop(system, None, TOOLS, self._handle_tool)

    def _run_loop(self, system_prompt, initial_message, tools, tool_handler):
        if initial_message and not self._messages:
            self._messages = [{"role": "user", "content": initial_message}]
        from raincurve.agents.base_agent import _get_llm_client
        import time

        provider_name, model, client = _get_llm_client()
        start = time.time()
        tool_call_count = 0

        while True:
            elapsed = time.time() - start
            if elapsed > self.MAX_WALLCLOCK_S:
                return AgentResult(
                    success=False,
                    failure_reason=f"Wallclock limit exceeded ({self.MAX_WALLCLOCK_S}s)",
                    duration_s=elapsed,
                    tool_call_count=tool_call_count,
                )
            if tool_call_count >= self.MAX_TOOL_CALLS:
                return AgentResult(
                    success=False,
                    failure_reason=f"Tool call limit exceeded ({self.MAX_TOOL_CALLS})",
                    duration_s=elapsed,
                    tool_call_count=tool_call_count,
                )

            if provider_name == "anthropic":
                resp = client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=self._messages,
                    tools=tools,
                )

                assistant_blocks = []
                tool_results = []
                done_result = None

                for block in resp.content:
                    if block.type == "text":
                        self._log(block.text)
                        assistant_blocks.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        tool_call_count += 1
                        assistant_blocks.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })

                        if block.name == "done":
                            done_result = block.input
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": "Acknowledged.",
                            })
                        else:
                            result = tool_handler(block.name, block.input)
                            result_str = result if isinstance(result, str) else json.dumps(result)
                            result_str = _truncate(result_str)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_str,
                            })

                self._messages.append({"role": "assistant", "content": assistant_blocks})
                if tool_results:
                    self._messages.append({"role": "user", "content": tool_results})

                if done_result is not None:
                    return AgentResult(
                        success=True,
                        output=done_result,
                        duration_s=time.time() - start,
                        tool_call_count=tool_call_count,
                    )

                if resp.stop_reason == "end_turn" and done_result is None:
                    self._messages.append({
                        "role": "user",
                        "content": "You stopped without calling `done`. If you're finished, call `done` with your verdict. Otherwise, continue.",
                    })

    _BLOCKING_PATTERNS = [
        "http.server", "SimpleHTTPServer", "flask run", "uvicorn", "gunicorn",
        "npm start", "npm run dev", "yarn dev", "node server",
        "python -m http", "python3 -m http",
        "tail -f", "watch ", "nodemon", "live-server",
    ]

    def _handle_tool(self, name: str, args: dict) -> str:
        if name == "bash":
            cmd = args["command"]
            timeout = args.get("timeout_s", 30)

            for pattern in self._BLOCKING_PATTERNS:
                if pattern in cmd:
                    return (
                        f"BLOCKED: '{pattern}' is a long-running process that will hang. "
                        f"Do NOT run servers on the host. Instead, run them inside a Docker "
                        f"container on the {self.network} network, or use the browser tool "
                        f"to interact with the app directly."
                    )

            self._log(f"  $ {cmd}")
            r = _exec_bash(cmd, self.project_dir, timeout)
            out = r.stdout + (f"\nSTDERR:\n{r.stderr}" if r.stderr else "")
            return f"exit_code={r.exit_code}\n{out or '(no output)'}"

        elif name == "browser":
            goal = args["goal"]
            login = args.get("login", True)
            max_steps = args.get("max_steps", 25)
            self._log(f"  [browser] {goal}")
            if self.on_browser:
                return self.on_browser({
                    "goal": goal,
                    "login": login,
                    "max_steps": max_steps,
                })
            return self._trigger_browser(goal, login, max_steps)

        elif name == "reconfigure":
            return self._handle_reconfigure(args)

        return f"Unknown tool: {name}"

    def _trigger_browser(self, goal: str, login: bool, max_steps: int) -> str:
        # Auto-start browser container if not running
        viewer = None
        container_name = None
        try:
            import docker
            from raincurve.browser.manager import ensure_browser, is_browser_running, CONTAINER_SUFFIX
            from raincurve.cli.cmd_sandbox import _short_id
            from raincurve.config import load_global_config
            import os as _os

            client = docker.from_env()
            container_prefix = f"rc-{_short_id(self.project_dir)}"
            container_name = f"{container_prefix}{CONTAINER_SUFFIX}"

            if not is_browser_running(client, container_prefix):
                cfg = load_global_config()
                api_key = cfg.llm.api_key or _os.environ.get("ANTHROPIC_API_KEY") or ""
                app_url = f"http://{container_prefix}:{self.app_port}"

                ensure_browser(
                    client=client,
                    container_prefix=container_prefix,
                    network=self.network,
                    app_url=app_url,
                    api_key=api_key,
                    project_dir=self.project_dir,
                    on_log=self._log,
                )
        except Exception as e:
            self._log(f"  [browser] Failed to start browser container: {e}")

        # Start live viewer (reuse across calls, stop previous if exists)
        if hasattr(self, '_viewer') and self._viewer:
            try:
                self._viewer.stop()
            except Exception:
                pass
        if container_name:
            try:
                from raincurve.browser.viewer import BrowserViewer
                self._viewer = BrowserViewer(container_name)
                self._viewer.start()
                viewer = self._viewer
                self._log(f"  [browser] Live view opened at http://localhost:19876")
            except Exception as e:
                self._log(f"  [browser] Viewer failed to start: {e}")

        import tempfile
        import os
        payload = json.dumps({
            "goal": goal,
            "login": login,
            "max_steps": max_steps,
        })

        # Write payload to temp file, mount into curl container
        payload_dir = tempfile.mkdtemp(prefix="raincurve-browser-payload-")
        payload_file = os.path.join(payload_dir, "payload.json")
        with open(payload_file, "w", encoding="utf-8") as f:
            f.write(payload)

        cmd = (
            f'docker run --rm --network {self.network} '
            f'-v "{payload_dir}:/payload:ro" '
            f'curlimages/curl:latest '
            f'curl -s -X POST http://browser-view:9000/scenario '
            f'-H "Content-Type: application/json" '
            f'-d @/payload/payload.json '
            f'--max-time 300'
        )
        self._log(f"  [browser] Triggering scenario...")
        r = _exec_bash(cmd, self.project_dir, timeout_s=320)

        if r.ok and r.stdout:
            try:
                result = json.loads(r.stdout)
                verdict = result.get("verdict", "unknown")
                summary = result.get("summary", "")
                steps = result.get("steps", [])
                return (
                    f"Browser scenario completed.\n"
                    f"Verdict: {verdict}\n"
                    f"Summary: {summary}\n"
                    f"Steps taken: {steps if isinstance(steps, int) else len(steps)}"
                )
            except json.JSONDecodeError:
                return f"Browser returned non-JSON:\n{r.stdout[:2000]}"
        return f"Browser scenario failed:\nexit_code={r.exit_code}\n{r.stdout}\n{r.stderr}"

    def _handle_reconfigure(self, args: dict) -> str:
        action = args.get("action", "")
        service = args.get("service_name", "")
        params = args.get("params", {})

        self._log(f"  [reconfigure] {action} {service}")

        if action == "add_container":
            image = params.get("image", "")
            memory = params.get("memory", "256m")
            cpus = params.get("cpus", "0.5")
            port = params.get("port", "")
            name = f"rc-{service}" if service else f"rc-custom-{id(params) % 10000}"

            port_flag = f"-p {port}:{port}" if port else ""
            env_flags = " ".join(f'-e {k}={v}' for k, v in params.get("env", {}).items())

            cmd = (
                f"docker run -d --name {name} --network {self.network} "
                f"--restart unless-stopped --memory={memory} --cpus={cpus} "
                f"{port_flag} {env_flags} {image}"
            )
            r = _exec_bash(cmd, self.project_dir, timeout_s=120)
            if r.ok:

                return f"Container {name} started successfully.\n{r.stdout}"
            return f"Failed to start container:\n{r.stderr}"

        elif action == "remove_container":
            cmd = f"docker stop {service} && docker rm {service}"
            r = _exec_bash(cmd, self.project_dir, timeout_s=30)
            if r.ok:

                return f"Container {service} removed."
            return f"Failed to remove container:\n{r.stderr}"

        elif action == "scale_replicas":
            replicas = params.get("replicas", 2)
            image = params.get("image", "")
            memory = params.get("memory", "512m")
            results = []

            # Find the original container to get its image
            if not image:
                r = _exec_bash(
                    f'docker inspect {service} --format "{{{{.Config.Image}}}}"',
                    self.project_dir, timeout_s=10,
                )
                image = r.stdout.strip() if r.ok else ""

            if not image:
                return f"Cannot determine image for {service}. Provide image in params."

            for i in range(1, replicas + 1):
                name = f"{service}-replica-{i}"
                cmd = (
                    f"docker run -d --name {name} --network {self.network} "
                    f"--restart unless-stopped --memory={memory} --cpus=0.5 {image}"
                )
                r = _exec_bash(cmd, self.project_dir, timeout_s=120)
                results.append(f"{name}: {'ok' if r.ok else 'failed'}")

            self._update_config(action, service, params)
            return "Scaled replicas:\n" + "\n".join(results)

        elif action == "add_load_balancer":
            upstream_services = params.get("upstreams", [service])
            listen_port = params.get("port", 80)

            upstream_block = "\\n".join(
                f"        server {s}:{listen_port};" for s in upstream_services
            )
            nginx_conf = (
                f"upstream backend {{\\n{upstream_block}\\n    }}\\n"
                f"    server {{\\n"
                f"        listen {listen_port};\\n"
                f"        location / {{\\n"
                f"            proxy_pass http://backend;\\n"
                f"        }}\\n"
                f"    }}"
            )

            cmd = (
                f'docker run -d --name rc-lb-{service} --network {self.network} '
                f'--restart unless-stopped --memory=64m --cpus=0.25 '
                f'-p {listen_port}:{listen_port} '
                f'nginx:alpine sh -c "echo -e \'events {{}} http {{ {nginx_conf} }}\' > /etc/nginx/nginx.conf && nginx -g \'daemon off;\'"'
            )
            r = _exec_bash(cmd, self.project_dir, timeout_s=60)
            if r.ok:

                return f"Load balancer started on port {listen_port} with upstreams: {upstream_services}"
            return f"Failed to start load balancer:\n{r.stderr}"

        elif action == "update_resources":
            memory = params.get("memory")
            cpus = params.get("cpus")
            if memory or cpus:
                update_flags = []
                if memory:
                    update_flags.append(f"--memory={memory}")
                if cpus:
                    update_flags.append(f"--cpus={cpus}")
                cmd = f"docker update {' '.join(update_flags)} {service}"
                r = _exec_bash(cmd, self.project_dir, timeout_s=10)
                if r.ok:
    
                    return f"Updated {service}: {', '.join(update_flags)}"
                return f"Failed to update resources:\n{r.stderr}"
            return "No resource changes specified."

        return f"Unknown reconfigure action: {action}"

