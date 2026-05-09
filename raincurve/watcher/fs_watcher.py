from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".raincurve", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "dist", "build", ".next",
}

IGNORE_EXTENSIONS = {".pyc", ".pyo", ".log", ".swp", ".swo"}

DEBOUNCE_S = 1.5


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[list[str]], None]) -> None:
        super().__init__()
        self._on_change = on_change
        self._pending: list[str] = []
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._rebuild_in_progress = False
        self._queued_after_rebuild = False

    def _should_ignore(self, path: str) -> bool:
        parts = Path(path).parts
        if any(p in IGNORE_DIRS for p in parts):
            return True
        if Path(path).suffix in IGNORE_EXTENSIONS:
            return True
        return False

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = event.src_path
        if not src or self._should_ignore(src):
            return

        with self._lock:
            self._pending.append(src)
            if self._rebuild_in_progress:
                self._queued_after_rebuild = True
                return
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_S, self._flush)
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            if self._rebuild_in_progress:
                self._queued_after_rebuild = True
                return
            files = list(set(self._pending))
            self._pending.clear()
            self._timer = None
            self._rebuild_in_progress = True

        if not files:
            self._finish_rebuild()
            return
        try:
            self._on_change(files)
        finally:
            self._finish_rebuild()

    def _finish_rebuild(self) -> None:
        with self._lock:
            self._rebuild_in_progress = False
            if self._queued_after_rebuild and self._pending:
                self._queued_after_rebuild = False
                self._timer = threading.Timer(DEBOUNCE_S, self._flush)
                self._timer.start()
            else:
                self._queued_after_rebuild = False


class FileWatcher:
    def __init__(self, project_dir: str, on_change: Callable[[list[str]], None]) -> None:
        self._project_dir = project_dir
        self._handler = _ChangeHandler(on_change)
        self._observer = Observer()

    def start(self) -> None:
        self._observer.schedule(self._handler, self._project_dir, recursive=True)
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)
