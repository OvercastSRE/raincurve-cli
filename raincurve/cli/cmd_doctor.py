from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import docker

from raincurve.auth.llm_auth import ensure_llm_key
from raincurve.config import load_global_config
from raincurve.ui.console import console, rc_error, rc_success, rc_warn


def _check(label: str, ok: bool, detail: str, warn: bool = False) -> bool:
    if ok:
        rc_success(f"{label:<30} {detail}")
    elif warn:
        rc_warn(f"{label:<30} {detail}")
    else:
        rc_error(f"{label:<30} {detail}")
    return ok


def run_doctor() -> bool:
    console.print("\n  [bold]raincurve doctor[/bold]\n")
    all_ok = True

    # Docker
    try:
        client = docker.from_env()
        info = client.info()
        version = client.version().get("Version", "?")
        _check("Docker", True, f"v{version}")
    except Exception:
        _check("Docker", False, "Not running or not installed", warn=False)
        console.print("    Try: Install Docker Desktop or start the Docker daemon", style="rc.dim")
        all_ok = False

    # Disk space
    try:
        usage = shutil.disk_usage(Path.home())
        free_gb = usage.free / (1024**3)
        ok = free_gb >= 10
        if not ok:
            all_ok = False
        _check("Disk space", ok, f"{free_gb:.1f}GB free (need >=10GB)", warn=not ok)
    except Exception:
        _check("Disk space", False, "Could not determine", warn=True)

    # RAM
    try:
        import psutil
        mem = psutil.virtual_memory()
        avail_gb = mem.available / (1024**3)
        total_gb = mem.total / (1024**3)
        ok = total_gb >= 12
        _check(
            "RAM",
            ok,
            f"{avail_gb:.1f}GB available / {total_gb:.1f}GB total",
            warn=not ok,
        )
        if not ok:
            all_ok = False
    except ImportError:
        _check("RAM", True, "psutil not installed, skipping check", warn=True)

    # Raincurve auth
    cfg = load_global_config()
    has_auth = bool(cfg.auth.access_token)
    detail = f"Logged in as {cfg.auth.email}" if has_auth else "Not logged in"
    _check("Raincurve auth", has_auth, detail, warn=not has_auth)

    # LLM auth
    has_llm = ensure_llm_key()
    provider = cfg.llm.provider or "not configured"
    _check("LLM auth", has_llm, provider if has_llm else f"{provider} - key not found", warn=not has_llm)

    # Snapshots
    rc_dir = Path.cwd() / ".raincurve" / "snapshots"
    if rc_dir.exists():
        snaps = [s for s in rc_dir.iterdir() if s.is_dir()]
        total_size = sum(f.stat().st_size for s in snaps for f in s.rglob("*") if f.is_file())
        size_mb = total_size / (1024**2)
        _check("Snapshots", True, f"{len(snaps)} snapshot(s), {size_mb:.1f}MB total")
    else:
        _check("Snapshots", True, "No snapshots yet", warn=True)

    console.print()
    return all_ok
