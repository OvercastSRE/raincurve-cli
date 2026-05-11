from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


IDEMPOTENT_TOOLS = frozenset({
    "read_file", "search_files", "list_directory",
    "context_read", "context_list",
})

WARN_IDENTICAL = 2
BLOCK_IDENTICAL = 5
WARN_FAILURES = 3
BLOCK_FAILURES = 8


@dataclass
class ToolGuardrails:
    _result_hashes: dict[str, list[str]] = field(default_factory=dict)
    _failure_counts: dict[str, int] = field(default_factory=dict)
    _tool_failures: dict[str, int] = field(default_factory=dict)

    def check(self, tool_name: str, args: dict, result: str) -> str | None:
        is_error = _looks_like_error(result)

        if is_error:
            key = _make_key(tool_name, args)
            self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
            self._tool_failures[tool_name] = self._tool_failures.get(tool_name, 0) + 1
            count = self._failure_counts[key]

            if count >= BLOCK_FAILURES:
                return (
                    f"BLOCKED: You have failed {count} times with the same "
                    f"{tool_name} call and arguments. Stop retrying and try a "
                    f"different approach."
                )
            if count >= WARN_FAILURES:
                return (
                    f"WARNING: This exact {tool_name} call has failed {count} times. "
                    f"Consider a different approach instead of retrying."
                )
        else:
            self._tool_failures[tool_name] = 0

        if _is_idempotent(tool_name, args):
            key = _make_key(tool_name, args)
            h = _hash_result(result)
            history = self._result_hashes.setdefault(key, [])
            history.append(h)

            if len(history) >= BLOCK_IDENTICAL:
                tail = history[-BLOCK_IDENTICAL:]
                if len(set(tail)) == 1:
                    return (
                        f"BLOCKED: You have called {tool_name} with the same arguments "
                        f"{BLOCK_IDENTICAL} times and gotten identical results each time. "
                        f"This information is already in your context. Move on."
                    )

            if len(history) >= WARN_IDENTICAL:
                tail = history[-WARN_IDENTICAL:]
                if len(set(tail)) == 1:
                    return (
                        f"WARNING: You have already read this exact same content "
                        f"{len(tail)} times. The result is identical — you already "
                        f"have this information. Do not re-read it."
                    )

        return None


def _is_idempotent(tool_name: str, args: dict) -> bool:
    if tool_name in IDEMPOTENT_TOOLS:
        return True
    if tool_name == "text_editor" and args.get("command") == "view":
        return True
    return False


def _make_key(tool_name: str, args: dict) -> str:
    stable = json.dumps(args, sort_keys=True, default=str)
    return f"{tool_name}:{stable}"


def _hash_result(result: str) -> str:
    return hashlib.md5(result.encode("utf-8", errors="replace")).hexdigest()


def _looks_like_error(result: str) -> bool:
    if not result:
        return False
    prefix = result[:500].lower()
    return any(s in prefix for s in (
        "error:", "error ", "not found", "exit_code=1",
        "failed", "traceback", "exception", "permission denied",
    ))
