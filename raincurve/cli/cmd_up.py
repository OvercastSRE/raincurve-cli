from __future__ import annotations

from pathlib import Path

import docker

from raincurve.cli.cmd_sandbox import _load_run_state, _save_run_state
from raincurve.config import load_project_config
from raincurve.docker.network import ensure_network
from raincurve.snapshot.manager import SnapshotManager
from raincurve.ui.console import rc_error, rc_print, rc_success


def run_up() -> None:
    project_dir = str(Path.cwd().resolve())

    state = _load_run_state(project_dir)
    config = load_project_config(project_dir)

    if not state or not state.snapshot_path:
        rc_error(
            "No snapshot found.",
            hint="Run `raincurve sandbox` to create a sandbox, or `raincurve down` to snapshot first.",
        )
        return

    project_name = config.project_name if config else "project"

    try:
        client = docker.from_env()
    except Exception:
        rc_error("Docker is not running.")
        return

    # Restore network
    rc_print("  Restoring sandbox from snapshot...")
    ensure_network(client, state.network_name)

    # Restore volumes and get image tags
    sm = SnapshotManager(project_dir, project_name)
    try:
        image_tags = sm.restore(state.snapshot_path)
    except FileNotFoundError as e:
        rc_error(str(e))
        return

    # Load saved env vars from project config
    env_vars = {}
    if config and hasattr(config, "env_var_defaults") and config.env_var_defaults:
        env_vars = dict(config.env_var_defaults)

    # Start containers from snapshot images
    for service_name, image_tag in image_tags.items():
        cs = state.containers.get(service_name)
        if not cs:
            continue

        # Only pass env vars to app containers, not to DB/infra containers
        container_env = env_vars if cs.host_port else None

        try:
            container = client.containers.run(
                image_tag,
                name=cs.container_id,
                network=state.network_name,
                ports={f"{cs.host_port}/tcp": cs.host_port} if cs.host_port else {},
                environment=container_env,
                detach=True,
                restart_policy={"Name": "unless-stopped"},
            )
            cs.container_id = container.id
            cs.status = "running"
            port_info = f" → localhost:{cs.host_port}" if cs.host_port else ""
            rc_success(f"  {service_name}{port_info}")
        except Exception as e:
            cs.status = "failed"
            cs.error = str(e)[:100]
            rc_error(f"  Failed to start {service_name}: {e}")

    _save_run_state(project_dir, state)

    rc_print(f"\n  Snapshot: {Path(state.snapshot_path).relative_to(project_dir)}", style="rc.dim")
    rc_success("\n  Sandbox restored!\n")
