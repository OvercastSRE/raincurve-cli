from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import docker

MAX_SNAPSHOTS = 3


class SnapshotManager:
    def __init__(self, project_dir: str, project_name: str) -> None:
        self._project_dir = Path(project_dir)
        self._project_name = project_name
        self._client = docker.from_env()
        self._snapshots_dir = self._project_dir / ".raincurve" / "snapshots"

    def capture(self, containers: dict[str, str]) -> str:
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
        snap_dir = self._snapshots_dir / ts
        snap_dir.mkdir(parents=True, exist_ok=True)

        manifest: dict = {"timestamp": ts, "images": {}, "volumes": {}}

        for service_name, container_id in containers.items():
            try:
                container = self._client.containers.get(container_id)
                tag = f"rc-{self._project_name}-{service_name}:snapshot-{ts}"
                container.commit(repository=tag.split(":")[0], tag=f"snapshot-{ts}")
                manifest["images"][service_name] = tag
            except docker.errors.NotFound:
                pass

        volumes_dir = snap_dir / "volumes"
        volumes_dir.mkdir(exist_ok=True)

        for volume in self._client.volumes.list(filters={"name": f"rc-{self._project_name}"}):
            vol_name = volume.name
            archive_name = f"{vol_name}.tar.gz"
            try:
                self._client.containers.run(
                    "alpine",
                    f"tar czf /out/{archive_name} -C /data .",
                    volumes={
                        vol_name: {"bind": "/data", "mode": "ro"},
                        str(volumes_dir): {"bind": "/out", "mode": "rw"},
                    },
                    remove=True,
                )
                manifest["volumes"][vol_name] = archive_name
            except docker.errors.APIError:
                pass

        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        self._prune()
        return str(snap_dir)

    def restore(self, snapshot_path: str) -> dict[str, str]:
        snap_dir = Path(snapshot_path)
        manifest_path = snap_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"No manifest at {manifest_path}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        image_tags: dict[str, str] = {}

        for service_name, tag in manifest.get("images", {}).items():
            image_tags[service_name] = tag

        volumes_dir = snap_dir / "volumes"
        for vol_name, archive in manifest.get("volumes", {}).items():
            archive_path = volumes_dir / archive
            if not archive_path.exists():
                continue
            try:
                self._client.volumes.get(vol_name)
            except docker.errors.NotFound:
                self._client.volumes.create(vol_name)

            self._client.containers.run(
                "alpine",
                f"tar xzf /out/{archive} -C /data",
                volumes={
                    vol_name: {"bind": "/data", "mode": "rw"},
                    str(volumes_dir): {"bind": "/out", "mode": "ro"},
                },
                remove=True,
            )

        return image_tags

    def latest_snapshot(self) -> str | None:
        if not self._snapshots_dir.exists():
            return None
        snaps = sorted(self._snapshots_dir.iterdir(), reverse=True)
        for s in snaps:
            if (s / "manifest.json").exists():
                return str(s)
        return None

    def _prune(self) -> None:
        if not self._snapshots_dir.exists():
            return
        snaps = sorted(self._snapshots_dir.iterdir(), reverse=True)
        for old in snaps[MAX_SNAPSHOTS:]:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
