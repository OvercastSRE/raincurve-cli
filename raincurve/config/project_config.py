from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from raincurve.config.schemas import ProjectConfig

RC_DIR_NAME = ".raincurve"
CONFIG_FILE = "config.json"
RUN_STATE_FILE = "run_state.json"


def _rc_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / RC_DIR_NAME


def load_project_config(project_dir: str | Path) -> ProjectConfig | None:
    path = _rc_dir(project_dir) / CONFIG_FILE
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ProjectConfig.model_validate(raw)


def save_project_config(project_dir: str | Path, config: ProjectConfig) -> None:
    rc = _rc_dir(project_dir)
    rc.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(rc)
    data = config.model_dump(mode="json")
    fd, tmp = tempfile.mkstemp(dir=str(rc), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(rc / CONFIG_FILE))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _ensure_gitignore(rc_dir: Path) -> None:
    gitignore = rc_dir.parent / ".gitignore"
    entries = {"snapshots/", "run_state.json"}
    rc_entries = {f".raincurve/{e}" for e in entries}

    existing_lines: set[str] = set()
    if gitignore.exists():
        existing_lines = {l.strip() for l in gitignore.read_text(encoding="utf-8").splitlines()}

    to_add = rc_entries - existing_lines
    if to_add:
        with gitignore.open("a", encoding="utf-8") as f:
            for entry in sorted(to_add):
                f.write(f"\n{entry}")
