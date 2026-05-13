from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from raincurve.agents.base_agent import AgentResult, OPENROUTER_BASE_URL, OPENROUTER_DEFAULT_KEY, _exec_bash
from raincurve.agents.code_analysis_agent import CodeAnalysisAgent
from raincurve.agents.environment_agent import EnvironmentAgent
from raincurve.agents.infra_agent import InfraAgent
from raincurve.agents.overseer_agent import OverseerAgent
from raincurve.agents.recovery_agent import RecoveryAgent
from raincurve.agents.seeder_agent import SeederAgent
from raincurve.agents.e2e_agent import E2EAgent
from raincurve.config import load_global_config
from raincurve.context.shared import SharedContext
from raincurve.models.code_context import (
    CodeContext,
    DatabaseInfo,
    ProjectDocs,
    SDKUsage,
)
from raincurve.pipe.server import PipeServer
from raincurve.services.recipes import build_docker_run_cmd, get_recipe, DISABLEABLE_SERVICES


@dataclass
class SandboxResult:
    success: bool
    port: int | None = None
    health_path: str = "/"
    services: list[dict] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    modifications: list[str] = field(default_factory=list)
    notes: str = ""
    test_credentials: dict = field(default_factory=dict)
    code_context: CodeContext | None = None
    seed_summary: dict = field(default_factory=dict)
    e2e_summary: dict = field(default_factory=dict)
    duration_s: float = 0.0
    tool_calls: int = 0
    failure_reason: str | None = None


DOC_FILENAMES = [
    "CLAUDE.md", "claude.md",
    ".cursorrules",
    "AGENTS.md", "agents.md",
    "CONTRIBUTING.md", "contributing.md",
    "README.md", "readme.md", "Readme.md",
    "DEVELOPMENT.md", "development.md",
    "docs/SETUP.md", "docs/setup.md",
]


class SandboxOrchestrator:
    """Thin state machine that chains specialist agents.

    Each step is an LLM agent with its own tool call budget.
    All agents share CodeContext. The orchestrator just passes
    results forward and handles retries.
    """

    def __init__(
        self,
        project_dir: str,
        project_name: str,
        container_name: str,
        network_name: str,
        env_overrides: dict[str, str],
        repo_brief: Any,
        on_log: Callable[[str], None],
    ) -> None:
        self.project_dir = project_dir
        self.project_name = project_name
        self.container_name = container_name
        self.network_name = network_name
        self.env_overrides = dict(env_overrides)
        self.repo_brief = repo_brief
        self.on_log = on_log
        self.pipe_server: PipeServer | None = None
        self.shared = SharedContext(project_dir)
        self._total_tool_calls = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Thread-safe helpers
    # ------------------------------------------------------------------

    def _record_tool_calls(self, count: int) -> None:
        with self._lock:
            self._total_tool_calls += count

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def run(self) -> SandboxResult:
        start = time.time()

        try:
            # Step 0: Read docs + inject keys (instant, Python)
            self.on_log("[Step 0] Reading project documentation...")
            self.docs = self._read_project_docs()
            self._inject_llm_keys()

            # Step 1: Essential infra (deterministic, no LLM — just postgres/redis)
            self.on_log("\n[Step 1] Starting essential infrastructure...")
            self._start_essential_infra()

            # Step 2: Build the application (this is the priority)
            self.on_log("\n[Step 2] Building application...")
            code_context = self._build_fallback_code_context(self.docs)
            build_result = self._run_overseer(code_context)

            output = build_result.output or {}
            port = output.get("port")

            # Step 3: Deep code analysis (LLM — now that app is up, enrich context)
            self.on_log("\n[Step 3] Analyzing codebase...")
            code_context = self._run_code_analysis(self.docs)
            self._persist_code_context(code_context)
            self._seed_shared_context(code_context)

            # Step 4: Seed data
            seed_summary: dict = {}
            if port:
                self.on_log("\n[Step 4] Seeding data...")
                seed_summary = self._run_seeding(code_context, output)

            # Step 5: External services (Pipe + Stripe/email/etc.)
            if port:
                self.on_log("\n[Step 5] Setting up external services...")
                self._start_pipe()
                self._run_external_services(code_context)

            # Step 6: E2E smoke tests
            e2e_summary: dict = {}
            if port:
                self.on_log("\n[Step 6] Running E2E smoke tests...")
                e2e_summary = self._run_e2e(code_context, output)

            return SandboxResult(
                success=True,
                port=port,
                health_path=output.get("health_path", "/"),
                services=output.get("services", []),
                env_vars=self.env_overrides,
                modifications=output.get("modifications", []),
                notes=output.get("notes", ""),
                test_credentials=output.get("test_credentials", {}),
                code_context=code_context,
                seed_summary=seed_summary,
                e2e_summary=e2e_summary,
                duration_s=time.time() - start,
                tool_calls=self._total_tool_calls,
            )

        except Exception as exc:
            self.on_log(f"\nOrchestrator error: {exc}")
            return SandboxResult(
                success=False,
                failure_reason=str(exc),
                duration_s=time.time() - start,
                tool_calls=self._total_tool_calls,
            )

    def stop(self) -> None:
        if self.pipe_server:
            self.pipe_server.stop()

    # ------------------------------------------------------------------
    # Step 0: Read project docs
    # ------------------------------------------------------------------

    def _read_project_docs(self) -> ProjectDocs:
        docs = ProjectDocs()
        root = Path(self.project_dir)

        attr_map = {
            "claude.md": "claude_md",
            "contributing.md": "contributing",
            "agents.md": "agents_md",
            ".cursorrules": "cursor_rules",
            "readme.md": "readme",
        }

        for filename in DOC_FILENAMES:
            path = root / filename
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:10000]
            except (PermissionError, OSError):
                continue

            key = filename.lower().split("/")[-1]
            attr = attr_map.get(key)
            if attr and getattr(docs, attr) is None:
                setattr(docs, attr, content)
                self.on_log(f"  Found {filename}")

        found = sum(1 for a in ["readme", "claude_md", "contributing", "agents_md", "cursor_rules"]
                     if getattr(docs, a))
        if found == 0:
            self.on_log("  No project documentation found")
        return docs

    # ------------------------------------------------------------------
    # Step 1: Code analysis
    # ------------------------------------------------------------------

    def _run_code_analysis(self, docs: ProjectDocs) -> CodeContext:
        cached = self._load_cached_code_context()
        if cached:
            self.on_log("  Using cached code context from .raincurve/code_context.json")
            return cached

        agent = CodeAnalysisAgent(
            project_dir=self.project_dir,
            project_name=self.project_name,
            repo_brief=self.repo_brief,
            project_docs=docs,
            on_log=lambda m: self.on_log(f"  [analysis] {m[:200]}"),
        )

        for attempt in range(3):
            result = agent.run()
            self._record_tool_calls(result.tool_call_count)
            if result.success and result.output:
                try:
                    ctx = CodeContext.model_validate({
                        **result.output,
                        "project_name": self.project_name,
                        "project_dir": self.project_dir,
                        "docs": docs.model_dump(),
                    })
                    self.on_log(
                        f"  Analysis complete: {len(ctx.sdk_usages)} services, "
                        f"{len(ctx.env_vars)} env vars, {len(ctx.api_routes)} routes "
                        f"({result.duration_s:.1f}s, {result.tool_call_count} calls)"
                    )
                    return ctx
                except Exception as e:
                    self.on_log(f"  CodeContext validation failed: {e}")

            self.on_log(
                f"  Code analysis attempt {attempt + 1}/3 failed: "
                f"{result.failure_reason or 'invalid output'}"
            )

        self.on_log("  Falling back to minimal code context")
        return self._build_fallback_code_context(docs)

    # ------------------------------------------------------------------
    # Shared context seeding
    # ------------------------------------------------------------------

    def _seed_shared_context(self, ctx: CodeContext) -> None:
        """Write analysis results to shared context so service agents can read it."""
        parts = [
            f"# Code Analysis — {ctx.project_name}",
            f"Language: {ctx.language}",
            f"Framework: {ctx.framework or 'unknown'}",
            f"Package manager: {ctx.package_manager or 'unknown'}",
        ]
        if ctx.app_description:
            parts.append(f"Description: {ctx.app_description}")
        if ctx.database:
            parts.append("\n## Database")
            parts.append(f"Type: {ctx.database.db_type}")
            if ctx.database.connection_env_var:
                parts.append(f"Connection env var: {ctx.database.connection_env_var}")
            if ctx.database.migration_command:
                parts.append(f"Migration: {ctx.database.migration_command}")
            if ctx.database.schema_summary:
                parts.append(f"Schema: {ctx.database.schema_summary}")
        if ctx.sdk_usages:
            parts.append("\n## Detected services")
            for sdk in ctx.sdk_usages:
                parts.append(
                    f"- {sdk.service_name}: package={sdk.sdk_package}, "
                    f"init={sdk.init_file}, files={sdk.files_using_sdk}"
                )
        if ctx.env_vars:
            parts.append("\n## Environment variables")
            for ev in ctx.env_vars:
                parts.append(f"- {ev.name}: {ev.purpose or '(no description)'}")
        if ctx.start_command:
            parts.append(f"\nStart command: {ctx.start_command}")
        if ctx.build_command:
            parts.append(f"Build command: {ctx.build_command}")
        if ctx.dockerfile_path:
            parts.append(f"Dockerfile: {ctx.dockerfile_path}")
        if ctx.compose_path:
            parts.append(f"Compose: {ctx.compose_path}")

        self.shared.write("analysis.md", "\n".join(parts))

        if self.env_overrides:
            import json
            self.shared.write("env_vars.json", json.dumps(self.env_overrides, indent=2))

        self.shared.write(
            "infra.md",
            f"# Infrastructure\n"
            f"App container: {self.container_name}\n"
            f"Network: {self.network_name}\n"
            f"Container prefix: {self.container_name}\n"
            f"Project: {self.project_name}\n",
        )
        self.on_log("  Seeded shared context for service agents")

    # ------------------------------------------------------------------
    # Step 2: Essential infra (deterministic, no LLM)
    # ------------------------------------------------------------------

    ESSENTIAL_SERVICES = {"postgres", "postgresql", "mysql", "redis", "mongodb"}

    def _start_essential_infra(self) -> None:
        """Start databases and caches using pre-baked recipes. No LLM needed.

        Only starts services that the project actually uses (detected via
        repo_brief or detection_result). Skips anything already running.
        """
        needed: set[str] = set()

        b = self.repo_brief
        if b and b.database_type:
            canonical = b.database_type.lower().replace("postgresql", "postgres")
            needed.add(canonical)

        if not needed:
            self.on_log("  No essential infrastructure detected")
            return

        self._infra_services: set[str] = set()

        for svc_name in sorted(needed):
            container = f"{self.container_name}-{svc_name}"

            check = _exec_bash(
                f'docker ps --filter "name=^{container}$" --format "{{{{.Status}}}}"',
                self.project_dir, 5,
            )
            if check.ok and check.stdout.strip() and "Up" in check.stdout:
                self.on_log(f"  {svc_name}: already running ({container})")
                self._infra_services.add(svc_name)
                recipe = get_recipe(svc_name)
                if recipe and recipe.env_wiring:
                    for k, v in recipe.env_wiring.items():
                        self.env_overrides.setdefault(k, v.replace("{name}", container))
                continue

            recipe = get_recipe(svc_name)
            if not recipe:
                self.on_log(f"  {svc_name}: no recipe available, skipping")
                continue

            cmd = build_docker_run_cmd(recipe, container, self.network_name, self.project_name)
            self.on_log(f"  {svc_name}: starting ({recipe.image})...")

            # Remove any stopped container with same name first
            _exec_bash(f"docker rm -f {container} 2>/dev/null", self.project_dir, 10)

            result = _exec_bash(cmd, self.project_dir, 60)
            if not result.ok:
                self.on_log(f"  {svc_name}: FAILED to start — {result.stderr[:200]}")
                continue

            # Wait for health check (up to 30s)
            import time as _time
            healthy = False
            for _ in range(15):
                _time.sleep(2)
                hc = _exec_bash(
                    f"docker exec {container} {recipe.healthcheck}",
                    self.project_dir, 10,
                )
                if hc.ok:
                    healthy = True
                    break

            if healthy:
                self.on_log(f"  {svc_name}: healthy ✓")
                self._infra_services.add(svc_name)
                if recipe.env_wiring:
                    for k, v in recipe.env_wiring.items():
                        self.env_overrides.setdefault(k, v.replace("{name}", container))
            else:
                self.on_log(f"  {svc_name}: started but health check failed")

    # ------------------------------------------------------------------
    # LLM key injection
    # ------------------------------------------------------------------

    def _inject_llm_keys(self) -> None:
        """Read the user's LLM keys from global config and inject them into
        env_overrides so the target app can use them."""
        cfg = load_global_config()
        llm = cfg.llm

        if llm.openrouter_api_key:
            self.env_overrides.setdefault("OPENROUTER_API_KEY", llm.openrouter_api_key)
            self.env_overrides.setdefault("OPENAI_API_KEY", llm.openrouter_api_key)
            self.env_overrides.setdefault("OPENAI_BASE_URL", OPENROUTER_BASE_URL)
            self.on_log("  Injected OpenRouter key (also as OpenAI-compatible fallback)")

        if llm.api_key:
            provider = (llm.provider or "").lower()
            if provider == "anthropic":
                self.env_overrides.setdefault("ANTHROPIC_API_KEY", llm.api_key)
                self.on_log("  Injected Anthropic API key")
            elif provider == "openai":
                self.env_overrides.setdefault("OPENAI_API_KEY", llm.api_key)
                self.on_log("  Injected OpenAI API key")
            else:
                self.env_overrides.setdefault("ANTHROPIC_API_KEY", llm.api_key)
                self.env_overrides.setdefault("OPENAI_API_KEY", llm.api_key)
                self.on_log(f"  Injected LLM API key (provider: {provider or 'unknown'})")

        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key:
            self.env_overrides.setdefault("ANTHROPIC_API_KEY", env_key)
        env_key = os.environ.get("OPENAI_API_KEY")
        if env_key:
            self.env_overrides.setdefault("OPENAI_API_KEY", env_key)
        env_key = os.environ.get("OPENROUTER_API_KEY")
        if env_key:
            self.env_overrides.setdefault("OPENROUTER_API_KEY", env_key)

    # ------------------------------------------------------------------
    # Pipe startup
    # ------------------------------------------------------------------

    def _start_pipe(self) -> None:
        """Start the Pipe LLM mock server. Always available — the InfraAgent
        decides which services to wire to it."""
        import openai as _openai

        cfg = load_global_config()
        api_key = (
            cfg.llm.openrouter_api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or OPENROUTER_DEFAULT_KEY
        )
        client = _openai.OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://raincurve.dev",
                "X-OpenRouter-Title": "raincurve-pipe",
            },
        )
        self.pipe_server = PipeServer(client=client, model="openai/gpt-5.4-nano")
        self.pipe_server.start()
        self.on_log(f"  Pipe server listening on port {self.pipe_server.port}")

    # ------------------------------------------------------------------
    # Step 5: External services (post-build, LLM agent)
    # ------------------------------------------------------------------

    def _run_external_services(self, ctx: CodeContext) -> None:
        """Set up non-essential external services AFTER the app is running.

        Essential infra (databases, caches) was already started deterministically
        in Step 2. This handles: HTTP API mocks (Stripe, Twilio), email services,
        search engines, etc.
        """
        already_handled = getattr(self, "_infra_services", set()) | DISABLEABLE_SERVICES
        remaining = []

        if not remaining:
            self.on_log("  No external services to set up")
            return

        self.on_log(f"  External services needed: {', '.join(s.name for s in remaining)}")

        summary_parts = [
            f"Language: {ctx.language}",
            f"Framework: {ctx.framework or 'unknown'}",
        ]
        for sdk in ctx.sdk_usages:
            strategy = (sdk.patching_strategy or "").lower()
            if not sdk.init_file or "false positive" in strategy or "not used" in strategy:
                summary_parts.append(f"Service {sdk.service_name}: NOT USED (skip)")
            else:
                summary_parts.append(
                    f"Service {sdk.service_name}: init in {sdk.init_file}, "
                    f"strategy: {sdk.patching_strategy}"
                )

        recipes_parts = []
        for svc in remaining:
            recipe = get_recipe(svc.name)
            if recipe:
                container = f"{self.container_name}-{svc.name}"
                cmd = build_docker_run_cmd(recipe, container, self.network_name, self.project_name)
                recipes_parts.append(f"\n**{svc.name}**:\n```\n{cmd}\n```")
                if recipe.env_wiring:
                    wiring = ", ".join(f"{k}={v}" for k, v in recipe.env_wiring.items())
                    recipes_parts.append(f"App env vars: {wiring}")

        pipe_port = self.pipe_server.port if self.pipe_server else 19877
        pipe_info = (
            f"Pipe (LLM-backed API mock) is running on host port {pipe_port}. "
            f"From inside Docker containers, it's at http://host.docker.internal:{pipe_port}. "
            f"For any HTTP API service (Stripe, Twilio, SendGrid, Resend, etc.), point "
            f"the SDK's base URL at Pipe instead of starting a container. "
            f"Example: STRIPE_API_BASE=http://host.docker.internal:{pipe_port}/stripe"
        )

        disabled_parts = []

        agent = InfraAgent(
            project_dir=self.project_dir,
            project_name=self.project_name,
            container_name=self.container_name,
            network_name=self.network_name,
            code_context_summary="\n".join(summary_parts),
            recipes_text="\n".join(recipes_parts) if recipes_parts else "(no recipes)",
            pipe_info=pipe_info,
            disabled_services="\n".join(disabled_parts) if disabled_parts else "(none)",
            project_docs=getattr(self, "docs", None),
            on_log=lambda m: self.on_log(f"  [services] {m}"),
        )

        result = agent.run()
        self._record_tool_calls(result.tool_call_count)

        if result.success and result.output:
            self.env_overrides.update(result.output.get("env_vars_for_app", {}))
            for s in result.output.get("services", []):
                if s.get("status") in ("running", "pipe", "disabled"):
                    self._infra_services.add(s.get("name", ""))
            self.on_log(
                f"  [services] Done ({result.duration_s:.1f}s, "
                f"{result.tool_call_count} calls)"
            )
        else:
            self.on_log(f"  [services] Incomplete: {result.failure_reason} — continuing")

    # ------------------------------------------------------------------
    # Step 3: Overseer (strategic build agent)
    # ------------------------------------------------------------------

    def _run_overseer(self, ctx: CodeContext) -> AgentResult:
        infra_services = set()
        if hasattr(self, '_infra_services'):
            infra_services = self._infra_services

        overseer = OverseerAgent(
            project_dir=self.project_dir,
            project_name=self.project_name,
            container_name=self.container_name,
            network_name=self.network_name,
            env_overrides=self.env_overrides,
            repo_brief=self.repo_brief,
            pre_started_services=infra_services,
            project_docs=self.docs,
            on_log=lambda m: self.on_log(f"  [overseer] {m}"),
        )

        result = overseer.run()
        self._record_tool_calls(result.tool_call_count)

        if result.success:
            self.on_log(
                f"  Build complete ({result.duration_s:.1f}s, "
                f"{result.tool_call_count} calls)"
            )
            return result

        raise RuntimeError(
            f"Overseer exhausted all strategies: {result.failure_reason or 'unknown'}"
        )

    # ------------------------------------------------------------------
    # Step 3 (legacy): Direct build without overseer
    # ------------------------------------------------------------------

    def _run_build(self, ctx: CodeContext) -> AgentResult:
        infra_services = set()
        if hasattr(self, '_infra_services'):
            infra_services = self._infra_services

        for attempt in range(3):
            agent = EnvironmentAgent(
                project_dir=self.project_dir,
                project_name=self.project_name,
                container_name=self.container_name,
                network_name=self.network_name,
                env_overrides=self.env_overrides,
                repo_brief=self.repo_brief,
                on_log=lambda m: self.on_log(f"  {m}"),
                pre_started_services=infra_services,
                pipe_handled_services=set(),
                project_docs=self.docs,
            )

            result = agent.run()
            self._record_tool_calls(result.tool_call_count)

            if result.success:
                self.on_log(
                    f"  Build complete ({result.duration_s:.1f}s, "
                    f"{result.tool_call_count} calls)"
                )
                return result

            failure = result.failure_reason or ""
            self.on_log(f"  Build attempt {attempt + 1}/3 failed: {failure[:200]}")

            if any(kw in failure.lower() for kw in ["build", "dockerfile", "compile", "install"]):
                recovery = RecoveryAgent(
                    project_dir=self.project_dir,
                    dockerfile_path=ctx.dockerfile_path or "Dockerfile",
                    error_logs=failure[:8000],
                    on_log=lambda m: self.on_log(f"  [recovery] {m}"),
                )
                fix = recovery.run()
                self._record_tool_calls(fix.tool_call_count)
                if fix.success:
                    self.on_log("  Recovery patch applied, retrying...")

        raise RuntimeError(f"Build failed after 3 attempts: {failure[:500]}")

    # ------------------------------------------------------------------
    # Step 5: Seed
    # ------------------------------------------------------------------

    def _run_seeding(self, ctx: CodeContext, build_output: dict) -> dict:
        port = build_output.get("port")
        if not port:
            return {"seeded": False, "reason": "No port"}

        # Find DB container from build output
        services = build_output.get("services", [])
        db_container = ""
        for svc in services:
            name = svc.get("name", "").lower()
            image = svc.get("image", "").lower()
            if any(db in name + image for db in ["postgres", "mysql", "mariadb", "mongo"]):
                db_container = svc.get("container_name", "")
                break

        # Get DB credentials from CodeContext + env_overrides
        db_user = "postgres"
        db_name = "app"
        if ctx.database and ctx.database.connection_env_var:
            db_url = self.env_overrides.get(ctx.database.connection_env_var, "")
            if db_url:
                parts = db_url.rsplit("/", 1)
                if len(parts) == 2 and parts[1]:
                    db_name = parts[1].split("?")[0]
                if "://" in db_url:
                    user_part = db_url.split("://")[1].split(":")[0]
                    if user_part:
                        db_user = user_part

        login_info = self._build_login_info(ctx, build_output)

        # Build schema context from CodeContext instead of re-extracting
        schema_context = ""
        if ctx.database:
            schema_context = f"Database: {ctx.database.db_type}\n"
            if ctx.database.schema_summary:
                schema_context += f"Schema: {ctx.database.schema_summary}\n"
            if ctx.database.business_domain:
                schema_context += f"Domain: {ctx.database.business_domain}\n"
        if ctx.core_entities:
            schema_context += f"Core entities: {', '.join(ctx.core_entities)}\n"
        if ctx.app_description:
            schema_context += f"App: {ctx.app_description}\n"

        seeder = SeederAgent(
            project_dir=self.project_dir,
            app_port=port,
            db_container=db_container,
            db_user=db_user,
            db_name=db_name,
            network=self.network_name,
            login_info=login_info,
            on_log=lambda m: self.on_log(f"  [seed] {m[:150]}"),
        )

        for attempt in range(2):
            result = seeder.run()
            self._record_tool_calls(result.tool_call_count)
            if result.success:
                summary = (result.output or {}).get("summary", "")
                self.on_log(f"  Seeding complete ({result.duration_s:.1f}s)")
                if summary:
                    self.on_log(f"  {summary}")
                return {"seeded": True, "duration_s": result.duration_s}
            self.on_log(f"  Seeding attempt {attempt + 1}/2 failed: {result.failure_reason}")

        return {"seeded": False, "reason": "Failed after 2 attempts"}

    # ------------------------------------------------------------------
    # Step 6: E2E
    # ------------------------------------------------------------------

    def _run_e2e(self, ctx: CodeContext, build_output: dict) -> dict:
        port = build_output.get("port")
        if not port:
            return {"passed": 0, "total": 0}

        login_info = self._build_login_info(ctx, build_output)

        routes_ctx = ""
        if ctx.api_routes:
            routes_ctx = "\n".join(
                f"  {r.method} {r.path} — {r.description}" for r in ctx.api_routes[:20]
            )
        if ctx.core_user_flows:
            routes_ctx += "\n\nCore user flows:\n" + "\n".join(
                f"  - {f}" for f in ctx.core_user_flows
            )

        e2e = E2EAgent(
            project_dir=self.project_dir,
            app_port=port,
            login_info=login_info,
            routes_context=routes_ctx[:5000],
            on_log=lambda m: self.on_log(f"  [e2e] {m[:150]}"),
        )

        result = e2e.run()
        self._record_tool_calls(result.tool_call_count)

        if result.success:
            output = result.output or {}
            journeys = output.get("journeys", [])
            passed = sum(1 for j in journeys if j.get("passed"))
            total = len(journeys)
            self.on_log(f"  E2E: {passed}/{total} journeys passed ({result.duration_s:.1f}s)")
            return {"passed": passed, "total": total, "duration_s": result.duration_s}

        self.on_log(f"  E2E incomplete: {result.failure_reason}")
        return {"passed": 0, "total": 0, "reason": result.failure_reason}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_login_info(self, ctx: CodeContext, build_output: dict) -> str:
        test_creds = build_output.get("test_credentials", {})
        if test_creds:
            parts = []
            if test_creds.get("username"):
                parts.append(f"Username: {test_creds['username']}")
            if test_creds.get("password"):
                parts.append(f"Password: {test_creds['password']}")
            if test_creds.get("login_path"):
                parts.append(f"Login: POST {test_creds['login_path']}")
            return "\n".join(parts)
        if ctx.auth_flow and ctx.auth_flow.default_credentials:
            creds = ctx.auth_flow.default_credentials
            info = f"Username: {creds.get('email', creds.get('username', ''))}\n"
            info += f"Password: {creds.get('password', '')}"
            if ctx.auth_flow.login_endpoint:
                info += f"\nLogin: POST {ctx.auth_flow.login_endpoint}"
            return info
        return ""

    def _build_fallback_code_context(self, docs: ProjectDocs) -> CodeContext:
        sdk_usages = []

        b = self.repo_brief
        db = None
        if b and b.database_type:
            db = DatabaseInfo(
                db_type=b.database_type,
                migration_tool=b.migration_tool,
                migration_command=b.migration_command,
                seed_command=b.seed_command,
            )

        return CodeContext(
            project_name=self.project_name,
            project_dir=self.project_dir,
            language=b.language if b else "unknown",
            framework=b.framework if b else None,
            package_manager=b.package_manager if b else None,
            docs=docs,
            sdk_usages=sdk_usages,
            database=db,
            start_command=b.start_command if b else None,
            build_command=b.build_command if b else None,
            dockerfile_path=b.dockerfile_path if b else None,
            compose_path=b.compose_path if b else None,
        )

    def _load_cached_code_context(self) -> CodeContext | None:
        path = Path(self.project_dir) / ".raincurve" / "code_context.json"
        if not path.exists():
            return None
        try:
            return CodeContext.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _persist_code_context(self, ctx: CodeContext) -> None:
        rc_dir = Path(self.project_dir) / ".raincurve"
        rc_dir.mkdir(parents=True, exist_ok=True)
        (rc_dir / "code_context.json").write_text(
            ctx.model_dump_json(indent=2), encoding="utf-8"
        )
