from __future__ import annotations

import json
import re
import time
from pathlib import Path


class EpisodicMemory:
    """Session-scoped event log. JSONL in .raincurve/memory/episodes/"""

    def __init__(self, project_dir: str, session_id: str) -> None:
        self.dir = Path(project_dir) / ".raincurve" / "memory" / "episodes"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{session_id}.jsonl"
        self._count = 0
        self._error_count = 0
        self._reflection_count = 0

    def record(self, event_type: str, data: dict) -> None:
        entry = {"ts": time.time(), "type": event_type, **data}
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass
        self._count += 1
        if data.get("is_error"):
            self._error_count += 1
        if event_type == "reflection":
            self._reflection_count += 1

    def recent(self, n: int = 10) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            entries = []
            for line in lines[-n:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            return entries
        except OSError:
            return []

    def summary(self) -> str:
        parts = [f"{self._count} tool calls"]
        if self._error_count:
            parts.append(f"{self._error_count} errors")
        if self._reflection_count:
            parts.append(f"{self._reflection_count} reflections")
        return ", ".join(parts)


class ProceduralMemory:
    """Stack-specific skills. Markdown in ~/.raincurve/skills/"""

    def __init__(self) -> None:
        self.dir = Path.home() / ".raincurve" / "skills"
        self.dir.mkdir(parents=True, exist_ok=True)

    def load(self, stack_key: str) -> str | None:
        path = self.dir / f"{stack_key}.md"
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                return content[:3000]
            except OSError:
                pass
        return None

    def save(self, stack_key: str, content: str) -> None:
        path = self.dir / f"{stack_key}.md"
        try:
            path.write_text(content, encoding="utf-8")
        except OSError:
            pass


class DeclarativeMemory:
    """Project facts. JSON in .raincurve/memory/facts.json"""

    _FACT_PATTERN = re.compile(r"^(?:Fact|Note|Remember):\s*(.+)", re.IGNORECASE)

    def __init__(self, project_dir: str) -> None:
        self.dir = Path(project_dir) / ".raincurve" / "memory"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "facts.json"
        self._facts = self._load()

    def _load(self) -> dict[str, str]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._facts, indent=2), encoding="utf-8")
        except OSError:
            pass

    def get(self, key: str) -> str | None:
        return self._facts.get(key)

    def set(self, key: str, value: str) -> None:
        self._facts[key] = value
        self._save()

    def all_facts(self) -> dict[str, str]:
        return dict(self._facts)

    def extract_from_text(self, text: str) -> None:
        for line in text.splitlines():
            m = self._FACT_PATTERN.match(line.strip())
            if m:
                fact = m.group(1).strip()
                key = fact[:60].lower().replace(" ", "_")
                self._facts[key] = fact
        if self._facts:
            self._save()
