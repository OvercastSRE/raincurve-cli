"""Shared context system for inter-agent communication.

Agents read and write markdown/JSON files in .raincurve/context/.
No rigid schema — agents write whatever they think is useful.
Other agents read it to understand what's been done and what's needed.

The event log (events.log) is a special append-only file. Every agent
writes timestamped entries. Agents can poll for new entries since their
last read — this is how they learn about other agents' progress without
having to actively check context files.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class SharedContext:
    def __init__(self, project_dir: str) -> None:
        self.root = Path(project_dir) / ".raincurve" / "context"
        self.root.mkdir(parents=True, exist_ok=True)
        self._event_lock = threading.Lock()

    def read(self, filename: str) -> str:
        path = self.root / filename
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def write(self, filename: str, content: str) -> None:
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def append(self, filename: str, content: str) -> None:
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._event_lock:
            existing = ""
            if path.exists():
                existing = path.read_text(encoding="utf-8", errors="replace")
            path.write_text(existing + "\n" + content, encoding="utf-8")

    def list_files(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            str(p.relative_to(self.root)).replace("\\", "/")
            for p in self.root.rglob("*")
            if p.is_file()
        )

    def read_all(self) -> str:
        files = self.list_files()
        if not files:
            return "(no shared context yet)"
        parts = []
        for f in files:
            if f == "events.log":
                continue
            content = self.read(f)
            if content.strip():
                parts.append(f"## {f}\n{content.strip()}")
        return "\n\n".join(parts) if parts else "(no shared context yet)"

    # ------------------------------------------------------------------
    # Event log — append-only, thread-safe, pollable
    # ------------------------------------------------------------------

    def log_event(self, agent_name: str, event: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{agent_name}] {event}"
        self.append("events.log", line)

    def get_events_since(self, last_line_count: int) -> tuple[list[str], int]:
        """Return new event lines since last_line_count and the new count."""
        content = self.read("events.log")
        if not content.strip():
            return [], 0
        lines = [l for l in content.strip().splitlines() if l.strip()]
        total = len(lines)
        if total <= last_line_count:
            return [], total
        new_lines = lines[last_line_count:]
        return new_lines, total
