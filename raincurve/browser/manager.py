from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import docker

from raincurve.agents.base_agent import _exec_bash
from raincurve.browser import container_script as _script_module

BROWSER_IMAGE_TAG = "rc-playwright:local"
CONTAINER_SUFFIX = "-browser"
SCENARIO_PORT = 9000

DOCKERFILE_CONTENT = """\
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
        curl wget gnupg libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \\
        libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 \\
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \\
        libpango-1.0-0 libcairo2 libasound2 libwayland-client0 \\
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright aiohttp anthropic
RUN playwright install chromium
WORKDIR /work
"""


def _ensure_image(client: docker.DockerClient, log: callable) -> None:
    """Build the Playwright image locally from Docker Hub base if not present."""
    try:
        client.images.get(BROWSER_IMAGE_TAG)
        return
    except docker.errors.ImageNotFound:
        pass

    log("  Building Playwright browser image (first time, ~2 min)...")
    build_dir = Path(tempfile.mkdtemp(prefix="raincurve-browser-build-"))
    dockerfile = build_dir / "Dockerfile"
    dockerfile.write_text(DOCKERFILE_CONTENT, encoding="utf-8")

    try:
        image, build_logs = client.images.build(
            path=str(build_dir),
            tag=BROWSER_IMAGE_TAG,
            rm=True,
            forcerm=True,
        )
        log("  Browser image built successfully.")
    except docker.errors.BuildError as e:
        logs = "\n".join(l.get("stream", l.get("error", "")) for l in e.build_log)
        raise RuntimeError(f"Browser image build failed:\n{logs}") from e


def ensure_browser(
    client: docker.DockerClient,
    container_prefix: str,
    network: str,
    app_url: str,
    api_key: str,
    project_dir: str,
    on_log: callable = None,
) -> str:
    log = on_log or (lambda s: None)
    name = f"{container_prefix}{CONTAINER_SUFFIX}"

    # Check if already running
    try:
        existing = client.containers.get(name)
        if existing.status == "running":
            log(f"  Browser container already running: {name}")
            return name
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass

    _ensure_image(client, log)

    # Write the container script to a temp directory
    script_source = inspect.getsource(_script_module)
    script_dir = Path(tempfile.mkdtemp(prefix="raincurve-browser-"))
    script_file = script_dir / "main.py"
    script_file.write_text(script_source, encoding="utf-8")

    log(f"  Starting browser container: {name}")

    container = client.containers.run(
        BROWSER_IMAGE_TAG,
        command='bash -c "python /work/main.py"',
        name=name,
        network=network,
        detach=True,
        restart_policy={"Name": "unless-stopped"},
        mem_limit="1g",
        nano_cpus=1_000_000_000,
        environment={
            "START_URL": app_url,
            "VIEWPORT_WIDTH": "1280",
            "VIEWPORT_HEIGHT": "800",
            "SCENARIO_PORT": str(SCENARIO_PORT),
            "ANTHROPIC_API_KEY": api_key,
            "PERSONA_MODEL": "claude-sonnet-4-6",
            "PERSONA_MAX_STEPS": "60",
        },
        volumes={
            str(script_dir): {"bind": "/work", "mode": "ro"},
        },
        labels={
            "rc-browser": "true",
        },
    )

    # Set network alias so chat agent can reach it as browser-view
    try:
        net = client.networks.get(network)
        net.disconnect(container)
        net.connect(container, aliases=["browser-view"])
    except Exception:
        pass

    # Wait for the scenario server to be ready
    log("  Waiting for browser to be ready...")
    import time
    for _ in range(30):
        time.sleep(2)
        try:
            logs = container.logs(tail=10).decode("utf-8", errors="replace")
            if "scenario server listening" in logs:
                log("  Browser ready!")
                return name
            if "ERROR:" in logs:
                error_line = [l for l in logs.splitlines() if "ERROR:" in l]
                if error_line:
                    log(f"  Browser error: {error_line[-1]}")
                    break
        except Exception:
            pass

    log("  Browser container started (may still be initializing)")
    return name


def stop_browser(client: docker.DockerClient, container_prefix: str) -> None:
    name = f"{container_prefix}{CONTAINER_SUFFIX}"
    try:
        container = client.containers.get(name)
        container.stop(timeout=5)
        container.remove()
    except docker.errors.NotFound:
        pass


def is_browser_running(client: docker.DockerClient, container_prefix: str) -> bool:
    name = f"{container_prefix}{CONTAINER_SUFFIX}"
    try:
        container = client.containers.get(name)
        return container.status == "running"
    except docker.errors.NotFound:
        return False
