import io
import sys

from rich.console import Console
from rich.theme import Theme

theme = Theme({
    "rc.info": "cyan",
    "rc.success": "green bold",
    "rc.warn": "yellow",
    "rc.error": "red bold",
    "rc.dim": "dim",
    "rc.highlight": "magenta bold",
})


def _make_console() -> Console:
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        try:
            wrapper = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            wrapper.reconfigure(line_buffering=True)
            return Console(theme=theme, file=wrapper)
        except Exception:
            pass
    return Console(theme=theme)


console = _make_console()


def _safe(msg: str) -> str:
    return msg.encode("utf-8", errors="replace").decode("utf-8")


def rc_print(msg: str, style: str = "rc.info") -> None:
    console.print(_safe(msg), style=style)


def rc_success(msg: str) -> None:
    console.print(_safe(f"  {msg}"), style="rc.success")


def rc_warn(msg: str) -> None:
    console.print(_safe(f"  {msg}"), style="rc.warn")


def rc_error(msg: str, hint: str | None = None) -> None:
    console.print(_safe(f"  {msg}"), style="rc.error")
    if hint:
        console.print(_safe(f"  Try: {hint}"), style="rc.dim")
