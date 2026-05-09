from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import docker
from rich.prompt import Prompt

from raincurve.agents.chat_agent import ChatAgent
from raincurve.cli.cmd_sandbox import _load_run_state, _short_id
from raincurve.config import load_global_config, load_project_config
from raincurve.ui.console import console, rc_error, rc_print, rc_success, rc_warn


def _get_sandbox_context(project_dir: str) -> dict | None:
    state = _load_run_state(project_dir)
    config = load_project_config(project_dir)
    if not state or not config:
        return None

    try:
        client = docker.from_env()
    except Exception:
        return None

    # Find app port
    app_port = None
    for name, cs in state.containers.items():
        if cs.host_port:
            app_port = cs.host_port
            break

    if not app_port:
        return None

    # Build container list
    containers_str = ""
    for name, cs in state.containers.items():
        port_info = f" -> localhost:{cs.host_port}" if cs.host_port else " (internal)"
        containers_str += f"  - {name}: {cs.container_id}{port_info}\n"

    # Find DB info
    db_info = "No database detected"
    for name, cs in state.containers.items():
        if any(db in name.lower() for db in ["postgres", "mysql", "mongo", "redis"]):
            db_user = config.env_var_defaults.get("POSTGRES_USER", "postgres")
            db_name = config.env_var_defaults.get("POSTGRES_DB", "app")
            db_info = f"docker exec {cs.container_id} psql -U {db_user} -d {db_name} -c '...'"
            break

    # Login info
    login_info = "Check project README for default credentials"
    if config.env_var_defaults:
        parts = []
        for k in ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]:
            if k in config.env_var_defaults:
                parts.append(f"{k}={config.env_var_defaults[k]}")
        if parts:
            db_info = f"docker exec {state.containers.get('postgres', list(state.containers.values())[0]).container_id} psql -U {config.env_var_defaults.get('POSTGRES_USER', 'postgres')} -d {config.env_var_defaults.get('POSTGRES_DB', 'app')} -c '...'"

    return {
        "app_port": app_port,
        "network": state.network_name,
        "containers": containers_str,
        "login_info": login_info,
        "db_info": db_info,
        "project_name": config.project_name,
    }


def _write_report(project_dir: str, result: dict) -> str | None:
    if not result:
        return None

    findings = result.get("findings", [])
    if not findings:
        return None

    reports_dir = Path(project_dir) / ".raincurve" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    report_path = reports_dir / f"{ts}.md"

    lines = [
        f"# Raincurve Chat Report",
        f"**Time:** {ts}",
        f"**Verdict:** {result.get('verdict', 'unknown')}",
        f"**Summary:** {result.get('summary', '')}",
        "",
    ]

    for i, f in enumerate(findings, 1):
        lines.append(f"## Finding {i}: {f.get('title', 'Untitled')}")
        lines.append(f"- **Severity:** {f.get('severity', 'unknown')}")
        if f.get("endpoint"):
            lines.append(f"- **Endpoint:** {f['endpoint']}")
        if f.get("evidence"):
            lines.append(f"- **Evidence:** {f['evidence']}")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return str(report_path)


def _json_result(result) -> dict:
    if not result.success:
        return {
            "success": False,
            "error": result.failure_reason,
            "duration_s": result.duration_s,
            "tool_calls": result.tool_call_count,
        }
    output = result.output or {}
    return {
        "success": True,
        "verdict": output.get("verdict", "unknown"),
        "summary": output.get("summary", ""),
        "findings": output.get("findings", []),
        "duration_s": result.duration_s,
        "tool_calls": result.tool_call_count,
    }


def run_chat(message: str | None = None, json_output: bool = False) -> None:
    project_dir = str(Path.cwd().resolve())

    ctx = _get_sandbox_context(project_dir)
    if not ctx:
        if json_output:
            print(json.dumps({"success": False, "error": "No running sandbox found"}))
            return
        rc_error(
            "No running sandbox found.",
            hint="Run `raincurve sandbox` first to spin up the environment.",
        )
        return

    if not json_output:
        rc_print(f"\n  [bold]raincurve chat[/bold] - {ctx['project_name']}")
        rc_print(f"  App: localhost:{ctx['app_port']} | Network: {ctx['network']}", style="rc.dim")

    def on_log(msg: str) -> None:
        if json_output:
            return
        if msg.startswith("  $"):
            rc_print(f"  {msg}", style="rc.dim")
        elif msg.startswith("  [browser]"):
            rc_print(f"  {msg}", style="rc.highlight")
        else:
            rc_print(f"  {msg}")

    agent = ChatAgent(
        project_dir=project_dir,
        app_port=ctx["app_port"],
        network=ctx["network"],
        containers=ctx["containers"],
        login_info=ctx["login_info"],
        db_info=ctx["db_info"],
        on_log=on_log,
    )

    if message:
        # One-shot mode
        result = agent.chat(message)
        if json_output:
            print(json.dumps(_json_result(result), indent=2))
        else:
            console.print()
            _print_result(result, project_dir)
    else:
        if json_output:
            print(json.dumps({"success": False, "error": "Interactive mode not supported with --json. Provide a message."}))
            return
        # Interactive mode — paste multi-line text, blank line to send
        rc_print("  Type your commands. Blank line to send, Ctrl+C to exit.\n", style="rc.dim")
        while True:
            try:
                lines: list[str] = []
                console.print("[cyan]>[/cyan] ", end="")
                while True:
                    line = input()
                    if line == "" and lines:
                        break
                    if line == "" and not lines:
                        continue
                    lines.append(line)
                user_input = "\n".join(lines)
                if user_input.strip().lower() in ("exit", "quit", "q"):
                    break

                console.print()
                result = agent.chat(user_input)
                _print_result(result, project_dir)
                console.print()

            except (KeyboardInterrupt, EOFError):
                break

        rc_print("\n  Session ended.\n", style="rc.dim")


def _print_result(result, project_dir: str) -> None:
    if not result.success:
        rc_error(f"Failed: {result.failure_reason}")
        return

    output = result.output or {}
    verdict = output.get("verdict", "unknown")
    summary = output.get("summary", "")
    findings = output.get("findings", [])

    console.print()

    verdict_style = {
        "succeeded": "rc.success",
        "found_issue": "rc.warn",
        "stuck": "rc.warn",
        "errored": "rc.error",
    }.get(verdict, "rc.info")

    rc_print(f"  [{verdict_style}]{verdict}[/{verdict_style}]: {summary}")

    if findings:
        console.print()
        for f in findings:
            sev = f.get("severity", "?")
            title = f.get("title", "?")
            rc_print(f"    [{sev}] {title}")
            if f.get("evidence"):
                rc_print(f"        {f['evidence'][:120]}", style="rc.dim")

    report_path = _write_report(project_dir, output)
    if report_path:
        rel = Path(report_path).relative_to(project_dir)
        rc_print(f"\n  Report: {rel}", style="rc.dim")

    rc_print(f"  Duration: {result.duration_s:.1f}s | Tool calls: {result.tool_call_count}", style="rc.dim")
