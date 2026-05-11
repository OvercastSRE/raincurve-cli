from __future__ import annotations

import json
from pathlib import Path

from raincurve.remote.connection import RemoteHost, CmdResult

REMOTE_BASE = "~/raincurve-sandboxes"


def check_prerequisites(conn: RemoteHost) -> dict:
    checks: dict = {"ssh": False, "docker": False, "git": False, "disk_gb": 0.0}

    r = conn.test()
    checks["ssh"] = r.ok
    if not r.ok:
        checks["ssh_error"] = r.stderr
        return checks

    r = conn.run("docker info --format '{{.ServerVersion}}' 2>/dev/null || echo MISSING")
    checks["docker"] = r.ok and "MISSING" not in r.stdout
    if checks["docker"]:
        checks["docker_version"] = r.stdout.strip().strip("'")

    r = conn.run("git --version 2>/dev/null || echo MISSING")
    checks["git"] = r.ok and "MISSING" not in r.stdout

    r = conn.run("df -BG --output=avail / 2>/dev/null | tail -1 || echo 0")
    try:
        checks["disk_gb"] = float(r.stdout.strip().replace("G", ""))
    except (ValueError, AttributeError):
        pass

    r = conn.run(
        "free -g 2>/dev/null | awk '/Mem:/{print $2}' || "
        "sysctl -n hw.memsize 2>/dev/null | awk '{print int($1/1073741824)}' || echo 0"
    )
    try:
        checks["ram_gb"] = int(r.stdout.strip())
    except (ValueError, AttributeError):
        checks["ram_gb"] = 0

    return checks


def install_docker(conn: RemoteHost) -> CmdResult:
    script = (
        "command -v docker >/dev/null 2>&1 && echo 'Docker already installed' && exit 0; "
        "curl -fsSL https://get.docker.com | sh && "
        "sudo usermod -aG docker $USER && "
        "echo 'Docker installed — you may need to reconnect for group changes'"
    )
    return conn.run(script, timeout=300)


def install_raincurve(conn: RemoteHost) -> CmdResult:
    script = (
        "command -v pip3 >/dev/null 2>&1 || "
        "{ command -v apt-get >/dev/null 2>&1 && sudo apt-get update -qq && "
        "sudo apt-get install -y -qq python3-pip python3-venv; } || "
        "{ command -v yum >/dev/null 2>&1 && sudo yum install -y python3-pip; }; "
        "pip3 install --user --upgrade raincurve 2>/dev/null || "
        "pip install --user --upgrade raincurve 2>/dev/null || "
        "python3 -m pip install --user --upgrade raincurve; "
        'echo "PATH=$HOME/.local/bin:$PATH" >> ~/.bashrc; '
        "export PATH=$HOME/.local/bin:$PATH; "
        "raincurve --version"
    )
    return conn.run(script, timeout=300)


def configure_llm(conn: RemoteHost, provider: str, api_key: str) -> CmdResult:
    config_dir = "~/.raincurve"
    config = {
        "version": 1,
        "llm": {"provider": provider, "api_key": api_key},
        "auth": {},
        "defaults": {},
        "trusted_paths": [],
    }
    escaped = json.dumps(json.dumps(config))
    script = (
        f"mkdir -p {config_dir} && "
        f"echo {escaped} > {config_dir}/global.json && "
        "echo 'LLM configured'"
    )
    return conn.run(script, timeout=30)


def clone_repo(conn: RemoteHost, repo_url: str) -> tuple[bool, str]:
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    target = f"{REMOTE_BASE}/{repo_name}"

    r = conn.run(f"mkdir -p {REMOTE_BASE}")
    if not r.ok:
        return False, f"Failed to create directory: {r.stderr}"

    exists = conn.run(f"test -d {target}/.git && echo EXISTS || echo NEW")
    if "EXISTS" in exists.stdout:
        r = conn.run(f"cd {target} && git pull --ff-only 2>&1 || true", timeout=120)
    else:
        r = conn.run(f"git clone {repo_url} {target} 2>&1", timeout=300)
    if not r.ok:
        return False, f"Git failed: {r.stdout}\n{r.stderr}"

    return True, target


def upload_env_file(conn: RemoteHost, local_path: str, remote_dir: str) -> CmdResult:
    return conn.upload(local_path, f"{remote_dir}/.env")


def upload_env_vars(conn: RemoteHost, env_vars: dict[str, str], remote_dir: str) -> CmdResult:
    lines = [f"{k}={v}" for k, v in env_vars.items()]
    content = "\\n".join(lines)
    return conn.run(f'printf "{content}" > {remote_dir}/.env')


def get_sandbox_status(conn: RemoteHost, remote_dir: str) -> dict | None:
    r = conn.run(f"cat {remote_dir}/.raincurve/run_state.json 2>/dev/null")
    if not r.ok or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def teardown_sandbox(conn: RemoteHost, remote_dir: str) -> CmdResult:
    return conn.run(
        f"cd {remote_dir} && export PATH=$HOME/.local/bin:$PATH && raincurve down 2>&1",
        timeout=120,
    )


def save_remote_config(host: RemoteHost, alias: str = "default") -> None:
    config_dir = Path.home() / ".raincurve"
    config_dir.mkdir(parents=True, exist_ok=True)
    remotes_path = config_dir / "remotes.json"

    remotes: dict = {}
    if remotes_path.exists():
        try:
            remotes = json.loads(remotes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    remotes[alias] = host.to_dict()
    remotes_path.write_text(json.dumps(remotes, indent=2), encoding="utf-8")


def load_remote_config(alias: str = "default") -> RemoteHost | None:
    remotes_path = Path.home() / ".raincurve" / "remotes.json"
    if not remotes_path.exists():
        return None
    try:
        remotes = json.loads(remotes_path.read_text(encoding="utf-8"))
        if alias in remotes:
            return RemoteHost.from_dict(remotes[alias])
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def save_active_project(remote_dir: str, alias: str = "default") -> None:
    config_dir = Path.home() / ".raincurve"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_path = config_dir / "remote_state.json"

    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    state[alias] = {"remote_dir": remote_dir}
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_active_project(alias: str = "default") -> str | None:
    state_path = Path.home() / ".raincurve" / "remote_state.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return state.get(alias, {}).get("remote_dir")
    except (json.JSONDecodeError, OSError):
        return None
