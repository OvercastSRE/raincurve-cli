from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from raincurve.agents.base_agent import AgentResult, OPENROUTER_BASE_URL, OPENROUTER_DEFAULT_KEY
from raincurve.agents.code_analysis_agent import CodeAnalysisAgent
from raincurve.agents.environment_agent import EnvironmentAgent
from raincurve.agents.infra_agent import InfraAgent
from raincurve.agents.recovery_agent import RecoveryAgent
from raincurve.agents.seeder_agent import SeederAgent
from raincurve.agents.e2e_agent import E2EAgent
from raincurve.config import load_global_config
from raincurve.models.code_context import (
    CodeContext,
    DatabaseInfo,
    ProjectDocs,
    SDKUsage,
)
from raincurve.pipe.server import PipeServer
from raincurve.services.recipes import build_docker_run_cmd, get_recipe, DISABLEABLE_SERVICES
from raincurve.stubs.detector import DetectionResult


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
        detection_result: DetectionResult,
        repo_brief: Any,
        on_log: Callable[[str], None],
    ) -> None:
        self.project_dir = project_dir
        self.project_name = project_name
        self.container_name = container_name
        self.network_name = network_name
        self.env_overrides = dict(env_overrides)
        self.detection_result = detection_result
        self.repo_brief = repo_brief
        self.on_log = on_log
        self.pipe_server: PipeServer | None = None
        self._total_tool_calls = 0

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def run(self) -> SandboxResult:
        start = time.time()

        try:
            # Step 0: Read project docs (Python, zero LLM)
            self.on_log("[Step 0] Reading project documentation...")
            docs = self._read_project_docs()

            # Step 1: Deep code analysis (LLM agent)
            self.on_log("\n[Step 1] Analyzing codebase...")
            code_context = self._run_code_analysis(docs)
            self._persist_code_context(code_context)

            # Start Pipe server (Python — raincurve infra, always available)
            self._start_pipe()

            # Step 3: Infrastructure + external services (LLM agent)
            self.on_log("\n[Step 3] Resolving external services and infrastructure...")
            infra_result = self._run_infra(code_context)
            if infra_result.success and infra_result.output:
                self.env_overrides.update(infra_result.output.get("env_vars_for_app", {}))

            # Step 4: Build and run app (LLM agent)
            self.on_log("\n[Step 4] Building application...")
            build_result = self._run_build(code_context)

            output = build_result.output or {}
            port = output.get("port")

            # Step 5: Seed data (LLM agent)
            seed_summary: dict = {}
            if port:
                self.on_log("\n[Step 5] Seeding data...")
                seed_summary = self._run_seeding(code_context, output)

            # Step 6: E2E smoke test (LLM agent)
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
            detection_result=self.detection_result,
            project_docs=docs,
            on_log=lambda m: self.on_log(f"  [analysis] {m[:200]}"),
        )

        for attempt in range(3):
            result = agent.run()
            self._total_tool_calls += result.tool_call_count
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
    # Step 3: Infrastructure + external services
    # ------------------------------------------------------------------

    def _run_infra(self, ctx: CodeContext) -> AgentResult:
        # Build context summary for the agent
        summary_parts = [
            f"Language: {ctx.language}",
            f"Framework: {ctx.framework or 'unknown'}",
        ]
        if ctx.database:
            summary_parts.append(f"Database: {ctx.database.db_type}")
            if ctx.database.connection_env_var:
                summary_parts.append(f"Connection env var: {ctx.database.connection_env_var}")
            if ctx.database.migration_command:
                summary_parts.append(f"Migration: {ctx.database.migration_command}")
        for sdk in ctx.sdk_usages:
            strategy = (sdk.patching_strategy or "").lower()
            if not sdk.init_file or "false positive" in strategy or "not used" in strategy:
                summary_parts.append(f"Service {sdk.service_name}: NOT USED (skip)")
            else:
                summary_parts.append(
                    f"Service {sdk.service_name}: init in {sdk.init_file}, "
                    f"strategy: {sdk.patching_strategy}"
                )

        # Build recipes reference
        recipes_parts = []
        if self.detection_result:
            for svc in self.detection_result.detected_services:
                if svc.name in DISABLEABLE_SERVICES:
                    continue
                recipe = get_recipe(svc.name)
                if recipe:
                    cmd = build_docker_run_cmd(
                        recipe, self.container_name, self.network_name, self.project_name,
                    )
                    recipes_parts.append(f"\n**{svc.name}**:\n```\n{cmd}\n```")
                    if recipe.env_wiring:
                        wiring = ", ".join(f"{k}={v}" for k, v in recipe.env_wiring.items())
                        recipes_parts.append(f"App env vars: {wiring}")

        # Pipe info
        pipe_port = self.pipe_server.port if self.pipe_server else 19877
        pipe_info = (
            f"Pipe (LLM-backed API mock) is running on host port {pipe_port}. "
            f"From inside Docker containers, it's at http://host.docker.internal:{pipe_port}. "
            f"For any HTTP API service (Stripe, Twilio, SendGrid, Resend, etc.), you can "
            f"point the SDK's base URL at Pipe instead of starting a mock container. "
            f"Example: STRIPE_API_BASE=http://host.docker.internal:{pipe_port}/stripe"
        )

        # Disableable services
        disabled = []
        if self.detection_result:
            for svc in self.detection_result.detected_services:
                if svc.name in DISABLEABLE_SERVICES:
                    disabled.append(f"- {svc.name} (disable via env var)")

        already_text = pipe_info
        if disabled:
            already_text += "\n\nDisableable services (analytics/monitoring — just disable):\n"
            already_text += "\n".join(disabled)

        agent = InfraAgent(
            project_dir=self.project_dir,
            project_name=self.project_name,
            container_name=self.container_name,
            network_name=self.network_name,
            code_context_summary="\n".join(summary_parts),
            recipes_text="\n".join(recipes_parts) if recipes_parts else "(no recipes)",
            already_handled=already_text,
            on_log=lambda m: self.on_log(f"  {m}"),
        )

        for attempt in range(2):
            result = agent.run()
            self._total_tool_calls += result.tool_call_count
            if result.success:
                self.on_log(
                    f"  Infrastructure ready ({result.duration_s:.1f}s, "
                    f"{result.tool_call_count} calls)"
                )
                return result
            self.on_log(f"  Infra attempt {attempt + 1}/2 failed: {result.failure_reason}")

        self.on_log("  Infrastructure incomplete — continuing anyway")
        return AgentResult(success=True, output={"services": [], "env_vars_for_app": {}})

    # ------------------------------------------------------------------
    # Step 4: Build
    # ------------------------------------------------------------------

    def _run_build(self, ctx: CodeContext) -> AgentResult:
        for attempt in range(3):
            agent = EnvironmentAgent(
                project_dir=self.project_dir,
                project_name=self.project_name,
                container_name=self.container_name,
                network_name=self.network_name,
                env_overrides=self.env_overrides,
                detection_result=self.detection_result,
                repo_brief=self.repo_brief,
                on_log=lambda m: self.on_log(f"  {m}"),
                pre_started_services=set(),
                pipe_handled_services=set(),
            )

            result = agent.run()
            self._total_tool_calls += result.tool_call_count

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
                self._total_tool_calls += fix.tool_call_count
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
            self._total_tool_calls += result.tool_call_count
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
        self._total_tool_calls += result.tool_call_count

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
        if self.detection_result:
            for svc_name, files in self.detection_result.import_hits.items():
                sdk_usages.append(SDKUsage(
                    service_name=svc_name,
                    sdk_package=svc_name,
                    init_file=files[0] if files else "",
                    files_using_sdk=files,
                    patching_strategy="Try env var override first, then patch code",
                ))

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
