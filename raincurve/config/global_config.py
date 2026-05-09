from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from raincurve.config.schemas import GlobalConfig

GLOBAL_DIR = Path.home() / ".raincurve"
GLOBAL_CONFIG_PATH = GLOBAL_DIR / "config.json"


def load_global_config() -> GlobalConfig:
    if not GLOBAL_CONFIG_PATH.exists():
        return GlobalConfig()
    raw = json.loads(GLOBAL_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return GlobalConfig.model_validate(raw)


def save_global_config(config: GlobalConfig) -> None:
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json")
    fd, tmp = tempfile.mkstemp(dir=str(GLOBAL_DIR), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(GLOBAL_CONFIG_PATH))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
