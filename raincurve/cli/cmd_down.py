from __future__ import annotations

from pathlib import Path

import docker

from raincurve.cli.cmd_sandbox import _load_run_state, _save_run_state
from raincurve.config import load_project_config
from raincurve.docker.network import remove_network
from raincurve.snapshot.manager import SnapshotManager
from raincurve.ui.console import rc_error, rc_print, rc_success


def run_down() -> None:
    project_dir = str(Path.cwd().resolve())

    state = _load_run_state(project_dir)
    if not state:
        rc_error("No running sandbox found.", hint="Run `raincurve sandbox` first.")
        return

    config = load_project_config(project_dir)
    project_name = config.project_name if config else "project"

    try:
        client = docker.from_env()
    except Exception:
        rc_error("Docker is not running.")
        return

    # Snapshot
    rc_print("  Snapshotting state...")
    sm = SnapshotManager(project_dir, project_name)
    container_map = {
        name: cs.container_id
        for name, cs in state.containers.items()
        if cs.container_id
    }

    try:
        snap_path = sm.capture(container_map)
        state.snapshot_path = snap_path
        rc_success(f"Snapshot saved → {Path(snap_path).relative_to(project_dir)}")
    except Exception as e:
        rc_print(f"  Snapshot failed: {e}", style="rc.warn")

    # Stop containers
    rc_print("  Stopping containers...")
    for name, cs in state.containers.items():
        try:
            container = client.containers.get(cs.container_id)
            container.stop(timeout=10)
            container.remove()
            rc_print(f"    Stopped {name}", style="rc.dim")
        except docker.errors.NotFound:
            pass
        except Exception as e:
            rc_print(f"    Error stopping {name}: {e}", style="rc.warn")

    # Also stop any aux containers (by label)
    try:
        aux_containers = client.containers.list(
            filters={"label": f"rc-aux-of={project_name}"}
        )
        for c in aux_containers:
            c.stop(timeout=10)
            c.remove()
            rc_print(f"    Stopped {c.name}", style="rc.dim")
    except Exception:
        pass

    # Stop any remaining containers still connected to the network
    try:
        net = client.networks.get(state.network_name)
        net.reload()
        for cid, _ in (net.attrs.get("Containers") or {}).items():
            try:
                c = client.containers.get(cid)
                c.stop(timeout=10)
                c.remove()
                rc_print(f"    Stopped {c.name}", style="rc.dim")
            except Exception:
                try:
                    net.disconnect(cid, force=True)
                except Exception:
                    pass
    except docker.errors.NotFound:
        pass
    except Exception:
        pass

    # Remove network
    try:
        remove_network(client, state.network_name)
        rc_print("    Removed network", style="rc.dim")
    except Exception as e:
        rc_print(f"    Network removal: {e}", style="rc.warn")

    # Update state
    for cs in state.containers.values():
        cs.status = "stopped"
    _save_run_state(project_dir, state)

    rc_success("Sandbox stopped.\n")
