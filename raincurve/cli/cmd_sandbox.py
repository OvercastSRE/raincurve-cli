from __future__ import annotations

import json
import re
import signal
import threading
from datetime import datetime
from pathlib import Path

import docker

from raincurve.auth.llm_auth import ensure_llm_key
from raincurve.config import load_project_config, save_project_config
from raincurve.config.schemas import ProjectConfig
from raincurve.docker.network import ensure_network
from raincurve.models.run_state import ContainerStatus, RunState
from raincurve.orchestrator import SandboxOrchestrator
from raincurve.stubs.detector import detect_external_services
from raincurve.utils.repo_analyzer import analyze_repo
from raincurve.ui.confirm import trust_check
from raincurve.ui.console import console, rc_error, rc_print, rc_success, rc_warn
from raincurve.watcher.fs_watcher import FileWatcher


def _short_id(project_dir: str) -> str:
    import hashlib

    return hashlib.md5(project_dir.encode()).hexdigest()[:12]


def _save_run_state(project_dir: str, state: RunState) -> None:
    rc_dir = Path(project_dir) / ".raincurve"
    rc_dir.mkdir(parents=True, exist_ok=True)
    (rc_dir / "run_state.json").write_text(
        state.model_dump_json(indent=2), encoding="utf-8"
    )


def run_sandbox(skip_trust: bool = False, json_output: bool = False) -> None:
    project_dir = str(Path.cwd().resolve())
    project_name = Path(project_dir).name.lower()
    project_name = re.sub(r"[^a-z0-9-]", "-", project_name).strip("-") or "project"

    # Trust check
    if not skip_trust and not trust_check(project_dir):
        rc_error("Folder not trusted. Exiting.")
        return

    # LLM key check
    if not ensure_llm_key():
        rc_error("No LLM provider configured.", hint="Run `raincurve init` first.")
        return

    short = _short_id(project_dir)
    container_name = f"rc-{short}"
    network_name = f"rc-{short}-net"

    rc_print(f"\n  [bold]Sandbox: {project_name}[/bold]")
    rc_print(f"  Directory: {project_dir}", style="rc.dim")
    rc_print(f"  Network: {network_name}", style="rc.dim")
    console.print()

    # Load saved env overrides
    existing_config = load_project_config(project_dir)
    env_overrides: dict[str, str] = {}
    if existing_config and existing_config.env_var_defaults:
        rc_print("  Using saved environment variables from .raincurve/config.json")
        env_overrides = dict(existing_config.env_var_defaults)

    # Docker check + network
    try:
        client = docker.from_env()
    except Exception:
        rc_error("Docker is not running.", hint="Start Docker Desktop or the Docker daemon.")
        return

    rc_print("  Creating Docker network...")
    ensure_network(client, network_name)

    # Deterministic pre-analysis
    rc_print("  Analyzing project structure...")
    repo_brief = analyze_repo(project_dir)
    if repo_brief.language != "unknown":
        stack_info = repo_brief.language
        if repo_brief.framework:
            stack_info += f" / {repo_brief.framework}"
        if repo_brief.package_manager:
            stack_info += f" ({repo_brief.package_manager})"
        rc_print(f"  Stack: {stack_info}")
    if repo_brief.has_compose:
        rc_print(f"  Compose: {repo_brief.compose_path}")
    if repo_brief.has_dockerfile:
        rc_print(f"  Dockerfile: {repo_brief.dockerfile_path}")
    if repo_brief.database_type:
        rc_print(f"  Database: {repo_brief.database_type}")

    # Detect external services
    rc_print("  Scanning for external dependencies...")
    detection = detect_external_services(project_dir)
    if detection.detected_services:
        svc_names = [s.name for s in detection.detected_services]
        rc_print(f"  Detected: {', '.join(svc_names)}")
    else:
        rc_print("  No external dependencies detected", style="rc.dim")

    console.print()

    # ------------------------------------------------------------------
    # Run the orchestrator — it handles everything from here
    # ------------------------------------------------------------------

    def on_log(msg: str) -> None:
        if not msg.strip():
            return
        if json_output:
            return
        if msg.startswith("[Step") or msg.startswith("[Phase"):
            console.print()
            rc_print(f"  [bold]{msg}[/bold]")
        elif msg.startswith("  Pipe:") or msg.startswith("  Analysis complete"):
            rc_print(f"  {msg}", style="rc.info")
        elif msg.startswith("  [analysis]") or msg.startswith("  [infra]"):
            rc_print(f"    {msg}", style="rc.dim")
        elif msg.startswith("  [overseer]"):
            rc_print(f"    {msg}", style="rc.info")
        elif msg.startswith("  [seed]") or msg.startswith("  [e2e]"):
            rc_print(f"    {msg}", style="rc.dim")
        elif msg.startswith("  [recovery]"):
            rc_print(f"    {msg}", style="rc.dim")
        elif msg.startswith("  Build attempt") or msg.startswith("  Seeding attempt"):
            rc_print(f"  {msg}", style="rc.warn")
        elif msg.startswith("  $ "):
            rc_print(f"    {msg}", style="rc.dim")
        elif msg.startswith("  Found "):
            rc_print(f"    {msg}", style="rc.dim")
        elif "complete" in msg.lower() or "resolved" in msg.lower():
            rc_print(f"  {msg}", style="rc.info")
        elif "failed" in msg.lower() or "error" in msg.lower():
            rc_print(f"  {msg}", style="rc.warn")
        else:
            rc_print(f"    {msg[:200]}", style="rc.dim")

    orchestrator = SandboxOrchestrator(
        project_dir=project_dir,
        project_name=project_name,
        container_name=container_name,
        network_name=network_name,
        env_overrides=env_overrides,
        detection_result=detection,
        repo_brief=repo_brief,
        on_log=on_log,
    )

    result = orchestrator.run()

    if not result.success:
        rc_error(f"Sandbox build failed: {result.failure_reason}")
        rc_print(
            f"  Duration: {result.duration_s:.1f}s, Tool calls: {result.tool_calls}",
            style="rc.dim",
        )
        orchestrator.stop()
        return

    # ------------------------------------------------------------------
    # Save state and config
    # ------------------------------------------------------------------

    run_state = RunState(
        project_dir=project_dir,
        network_name=network_name,
    )
    for svc in result.services:
        run_state.containers[svc.get("name", "unknown")] = ContainerStatus(
            service_name=svc.get("name", "unknown"),
            container_id=svc.get("container_name", ""),
            host_port=svc.get("host_port"),
            status="running",
        )
    _save_run_state(project_dir, run_state)

    flat_env: dict[str, str] = {}
    for k, v in result.env_vars.items():
        if isinstance(v, str):
            flat_env[k] = v
        elif isinstance(v, dict):
            for ek, ev in v.items():
                flat_env[ek] = str(ev)

    config = ProjectConfig(
        project_name=project_name,
        network_name=network_name,
        services=list(result.services),
        seed_strategy="none",
        env_var_defaults=flat_env,
        created_at=datetime.utcnow().isoformat(),
        last_sandbox_at=datetime.utcnow().isoformat(),
    )
    save_project_config(project_dir, config)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    if json_output:
        json_data = {
            "success": True,
            "project_name": project_name,
            "network": network_name,
            "services": [
                {
                    "name": svc.get("name", "?"),
                    "container_name": svc.get("container_name", ""),
                    "host_port": svc.get("host_port"),
                    "image": svc.get("image", ""),
                }
                for svc in result.services
            ],
            "app_port": result.port,
            "health_path": result.health_path,
            "env_vars": flat_env,
            "modifications": result.modifications,
            "notes": result.notes,
            "seed": result.seed_summary,
            "e2e": result.e2e_summary,
            "duration_s": result.duration_s,
            "tool_calls": result.tool_calls,
        }
        print(json.dumps(json_data, indent=2))
        orchestrator.stop()
        return

    console.print()
    rc_success("Sandbox is running!")
    console.print()

    for svc in result.services:
        name = svc.get("name", "?")
        port = svc.get("host_port")
        if port:
            rc_print(f"    {name:<20} → localhost:{port}")
        else:
            rc_print(f"    {name:<20} → (internal)")

    if result.modifications:
        console.print()
        rc_warn("Files modified:")
        for m in result.modifications:
            rc_print(f"    {m}", style="rc.dim")

    if result.notes:
        console.print()
        rc_print(f"  Notes: {result.notes}", style="rc.dim")

    rc_print(
        f"\n  Duration: {result.duration_s:.1f}s | Tool calls: {result.tool_calls}",
        style="rc.dim",
    )
    console.print()

    # ------------------------------------------------------------------
    # File watcher
    # ------------------------------------------------------------------

    rc_print(
        "  Open a new terminal in this directory and run "
        "[bold]raincurve chat[/bold] to talk to your sandbox.\n"
    )
    rc_print("  Watching for changes... (Ctrl+C to stop)\n", style="rc.dim")

    stop_event = threading.Event()

    def on_change(files: list[str]) -> None:
        short_files = [str(Path(f).relative_to(project_dir)) for f in files[:5]]
        if len(files) > 5:
            short_files.append(f"... and {len(files) - 5} more")
        rc_print(f"  Changed: {', '.join(short_files)}")

        infra_changed = any(
            any(kw in Path(f).name.lower() for kw in ["dockerfile", "compose", "docker-compose"])
            for f in files
        )

        if infra_changed:
            rc_print("  Infrastructure files changed — full rebuild...", style="rc.dim")
            try:
                rebuild_client = docker.from_env()
                for c in rebuild_client.containers.list(all=True):
                    if c.name.startswith(container_name):
                        c.stop(timeout=5)
                        c.remove(force=True)
            except Exception as e:
                rc_warn(f"  Teardown warning: {e}")

            rc_print("  Rebuilding...", style="rc.dim")
            from raincurve.agents.environment_agent import EnvironmentAgent

            rebuild_agent = EnvironmentAgent(
                project_dir=project_dir,
                project_name=project_name,
                container_name=container_name,
                network_name=network_name,
                env_overrides=env_overrides,
                detection_result=detection,
                repo_brief=repo_brief,
                on_log=lambda m: None,
            )
            try:
                rebuild_result = rebuild_agent.run()
            except Exception as exc:
                exc_str = str(exc)
                if "rate_limit" in exc_str:
                    rc_error("Rebuild skipped: API rate limit hit. Will retry on next change.")
                elif "credit balance is too low" in exc_str:
                    rc_error("Rebuild stopped: API credit balance exhausted.")
                    stop_event.set()
                else:
                    rc_error(f"Rebuild error: {exc_str[:200]}")
                return
            if rebuild_result.success:
                rc_success("Rebuilt successfully")
            else:
                rc_error(f"Rebuild failed: {rebuild_result.failure_reason}")
        else:
            rc_print("  Rebuilding app container (no LLM)...", style="rc.dim")
            import subprocess as _sp
            import os as _os

            build_env = dict(_os.environ)
            build_env["DOCKER_BUILDKIT"] = "1"

            build_r = _sp.run(
                ["docker", "build", "--progress=plain", "-t", f"{container_name}:latest", "."],
                cwd=project_dir,
                capture_output=True, text=True, timeout=180,
                env=build_env, encoding="utf-8", errors="replace",
            )
            if build_r.returncode != 0:
                rc_error(f"  Build failed: {build_r.stderr[:500]}")
                return

            inspect_r = _sp.run(
                [
                    "docker", "inspect", "--format",
                    "{{range .Config.Env}}{{println .}}{{end}}|||"
                    "{{range $k,$v := .NetworkSettings.Ports}}"
                    "{{$k}}={{range $v}}{{.HostPort}}{{end}} {{end}}",
                    container_name,
                ],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )

            _sp.run(
                ["docker", "stop", "-t", "5", container_name],
                capture_output=True, timeout=15,
            )
            _sp.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, timeout=10,
            )

            env_args: list[str] = []
            port_args: list[str] = []
            if inspect_r.returncode == 0 and "|||" in inspect_r.stdout:
                env_section, port_section = inspect_r.stdout.split("|||", 1)
                for line in env_section.strip().splitlines():
                    if line and "=" in line:
                        env_args.extend(["-e", line])
                for mapping in port_section.strip().split():
                    if "=" in mapping:
                        container_port, host_port = mapping.split("=", 1)
                        if host_port:
                            port_args.extend(
                                ["-p", f"{host_port}:{container_port.split('/')[0]}"]
                            )

            run_cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--network", network_name,
                f"--label=rc-aux-of={project_name}",
                "--restart=unless-stopped",
            ] + env_args + port_args + [f"{container_name}:latest"]

            run_r = _sp.run(
                run_cmd, capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            if run_r.returncode == 0:
                rc_success("App container rebuilt (no LLM)")
            else:
                rc_error(f"  Container start failed: {run_r.stderr[:300]}")

    watcher = FileWatcher(project_dir, on_change)
    watcher.start()

    def _signal_handler(sig: int, frame: object) -> None:
        console.print()
        rc_print("  Stopping watcher...")
        orchestrator.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)

    stop_event.wait()
    watcher.stop()
    rc_print("\n  Sandbox still running. Use `raincurve down` to stop and snapshot.\n")
