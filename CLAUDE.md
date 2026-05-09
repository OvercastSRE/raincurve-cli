# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Raincurve is a Python CLI that creates high-fidelity local production replicas in Docker. Given any codebase, it uses LLM agents to analyze the project, build Docker containers, start auxiliary services (databases, caches), seed data, and watch for file changes to auto-rebuild. Users can then interact with the running sandbox via chat (API-level) or a live browser viewer (UI-level).

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run CLI
raincurve --help
raincurve sandbox          # main command: analyze repo + spin up Docker sandbox
raincurve sandbox -y       # skip trust confirmation
raincurve sandbox -y --json  # structured JSON output (for CI/agents)
raincurve init             # configure LLM provider (Claude or OpenAI)
raincurve login            # authenticate with raincurve.dev
raincurve up               # restore sandbox from last snapshot
raincurve down             # snapshot + tear down running sandbox
raincurve doctor           # check Docker, disk, RAM, auth readiness
raincurve chat             # interactive chat with running sandbox
raincurve chat "message"   # one-shot mode
raincurve chat --json "message"  # structured JSON output (for CI/agents)

# Lint and type check
ruff check raincurve/
ruff format raincurve/
mypy raincurve/

# Tests
pytest
pytest tests/test_foo.py           # single file
pytest tests/test_foo.py::test_bar # single test
```

## Architecture

The CLI entry point is `raincurve/main.py` — a Typer app. Each subcommand is a thin wrapper in `raincurve/cli/cmd_*.py` that delegates to the appropriate subsystem.

### Agent System (`raincurve/agents/`)

The core of the product. LLM-powered agents that run in an agentic loop with tool use:

- **BaseAgent** (`base_agent.py`): Generic agent loop supporting both Anthropic and OpenAI APIs. Runs tool calls in a loop until the agent calls `done`, with wallclock and tool-call limits. Handles tool dispatch, output truncation, and verification of `done` results. API key is resolved from config → `ANTHROPIC_API_KEY` env var → error.
- **EnvironmentAgent** (`environment_agent.py`): The main agent. Scans the repo (via `repo_scanner`), writes Dockerfiles if none exist, starts containers, runs migrations, verifies health via HTTP. Has a large system prompt with Docker best practices for any stack.
- **RecoveryAgent** (`recovery_agent.py`): Fixes Docker build failures. Given error logs and a Dockerfile, it reads files, diagnoses the issue, and applies minimal patches.
- **SeederAgent** (`seeder_agent.py`): Populates a running sandbox with realistic data by hitting the app's API endpoints.
- **ChatAgent** (`chat_agent.py`): Interactive agent for the `raincurve chat` command. Has tools: `bash` (shell commands), `browser` (visual UI automation), `reconfigure` (add/remove containers, scale replicas, load balancers), and `done`. Blocking commands (http servers, watchers) are detected and rejected before execution. Default bash timeout is 30s.

Agents expose tools to the LLM and handle tool results locally. The `done` tool triggers verification before the agent loop exits. All agent prompts are OS-agnostic and codebase-agnostic.

### Browser System (`raincurve/browser/`)

Three components for visual UI interaction:

- **`manager.py`**: Builds and manages the Playwright browser container. The image (`rc-playwright:local`) is built locally from `python:3.12-slim` with Playwright + Chromium installed, avoiding dependency on `mcr.microsoft.com`. The container runs on the sandbox Docker network.
- **`container_script.py`**: Runs inside the Docker container. Uses **accessibility tree extraction** (not screenshots) to drive the browser. Extracts interactive DOM elements as a numbered list, sends text to Claude, Claude responds with selector-based actions (`click [4]`, `type_text [2] "admin"`). Screenshots are taken after each action and emitted as `FRAME:<b64png>` lines for the live viewer — but Claude never sees them. Includes tools: `click`, `type_text`, `select_option`, `navigate`, `scroll`, `press_key`, `wait`, `done`.
- **`viewer.py`**: Local HTTP server (port 19876) that streams `FRAME:` and `STEP:` lines from the browser container to a web page via polling. Uses Python's built-in `http.server.ThreadingHTTPServer` — no asyncio, no websockets. The server is a singleton that persists across chat messages. Opens automatically when the browser tool is triggered.

### External Service Stubs (`raincurve/stubs/`)

Detects external dependencies (Stripe, OpenAI, SendGrid, Twilio, AWS S3, etc.) by scanning source imports and `.env` files. Starts mock HTTP servers in Docker containers so the sandbox runs without real API keys. For LLM APIs (OpenAI/Anthropic), the user's real key is passed through instead of stubbing.

### Snapshot System (`raincurve/snapshot/`)

`SnapshotManager` commits running containers as images and archives Docker volumes to `.raincurve/snapshots/`. `raincurve down` captures; `raincurve up` restores. Keeps at most 3 snapshots (pruned automatically).

### Config (`raincurve/config/`)

Two config levels:
- **Global** (`~/.raincurve/config.json`): auth tokens, LLM provider/key, resource budgets, trusted paths.
- **Project** (`.raincurve/config.json`): project name, network, services, saved env vars. Auto-creates `.gitignore` entries for `snapshots/` and `run_state.json`.

Schemas are Pydantic models in `config/schemas.py`. Global budget defaults: 14GB RAM, 240GB disk, 20 containers max.

### File Watcher (`raincurve/watcher/`)

Uses `watchdog` to monitor the project directory. On changes, debounces (1.5s) then triggers an `EnvironmentAgent` rebuild. Ignores `.git`, `node_modules`, `__pycache__`, `.venv`, `.raincurve`, and common build dirs.

### UI (`raincurve/ui/`)

Rich-based console output with a custom theme (`rc.info`, `rc.success`, `rc.warn`, `rc.error`, `rc.dim`). `LivePanel` shows a real-time service status table during sandbox creation.

## Key Conventions

- Python 3.11+, Pydantic v2 for all models, Typer for CLI
- Ruff for linting/formatting (line length 100, target py311)
- Build system: Hatchling
- Docker container naming: `rc-{hash12}` for app, `rc-{hash12}-{service}` for aux
- Docker network naming: `rc-{hash12}-net`
- All containers labeled with `rc-aux-of={project_name}` for cleanup
- Port allocation range: 30000-39999
- Agent tool-call limit: 100 (EnvironmentAgent), 60 (ChatAgent), 20 (RecoveryAgent)
- Agent wallclock limit: 900s (EnvironmentAgent), 600s (ChatAgent), 120s (RecoveryAgent)
- Chat bash default timeout: 30s (blocking commands are rejected pre-execution)
- Browser viewer port: 19876
- All agent prompts must be OS-agnostic and codebase-agnostic
- Chat agent treats the project directory as read-only — all scripts run inside containers
- `--json` flag on `sandbox` and `chat` for machine-readable output (CI/agent integration)
