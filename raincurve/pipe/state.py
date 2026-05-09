from __future__ import annotations

import threading
from typing import Any


class StateStore:
    """Thread-safe in-memory store for mock API objects.

    Keyed by (api, resource_type, object_id).  Supports the full
    create / retrieve / list / delete lifecycle so that a ``POST`` followed
    by a ``GET`` for the same ID returns consistent data.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, api: str, resource_type: str, obj_id: str, obj: dict) -> None:
        key = f"{api}:{resource_type}"
        with self._lock:
            self._data.setdefault(key, {})[obj_id] = obj

    def get(self, api: str, resource_type: str, obj_id: str) -> dict | None:
        key = f"{api}:{resource_type}"
        with self._lock:
            return self._data.get(key, {}).get(obj_id)

    def list_all(self, api: str, resource_type: str) -> list[dict]:
        key = f"{api}:{resource_type}"
        with self._lock:
            return list(self._data.get(key, {}).values())

    def delete(self, api: str, resource_type: str, obj_id: str) -> bool:
        key = f"{api}:{resource_type}"
        with self._lock:
            bucket = self._data.get(key, {})
            return bucket.pop(obj_id, None) is not None

    def dump(self, api: str) -> dict[str, list[dict]]:
        prefix = f"{api}:"
        with self._lock:
            result: dict[str, list[dict]] = {}
            for key, objects in self._data.items():
                if key.startswith(prefix):
                    resource_type = key[len(prefix):]
                    result[resource_type] = list(objects.values())
            return result

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
