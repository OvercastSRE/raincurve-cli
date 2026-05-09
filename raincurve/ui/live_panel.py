from __future__ import annotations

from rich.live import Live
from rich.table import Table

from raincurve.models.run_state import RunState
from raincurve.ui.console import console

STATUS_STYLE = {
    "building": "yellow",
    "starting": "cyan",
    "healthy": "green bold",
    "running": "green",
    "failed": "red bold",
    "stopped": "dim",
    "pending": "dim",
}


def _build_table(state: RunState) -> Table:
    table = Table(show_header=True, header_style="bold cyan", padding=(0, 2))
    table.add_column("Service", style="bold")
    table.add_column("Status")
    table.add_column("Port")

    for name, cs in state.containers.items():
        style = STATUS_STYLE.get(cs.status, "")
        port = f"localhost:{cs.host_port}" if cs.host_port else "-"
        status_text = cs.status
        if cs.error:
            status_text += f" ({cs.error[:40]})"
        table.add_row(name, f"[{style}]{status_text}[/{style}]", port)

    return table


class LivePanel:
    def __init__(self, state: RunState) -> None:
        self._state = state
        self._live: Live | None = None

    def __enter__(self) -> LivePanel:
        self._live = Live(
            _build_table(self._state),
            console=console,
            refresh_per_second=4,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        if self._live:
            self._live.__exit__(*args)

    def refresh(self) -> None:
        if self._live:
            self._live.update(_build_table(self._state))

    def update_service(self, name: str, status: str, error: str | None = None) -> None:
        if name in self._state.containers:
            self._state.containers[name].status = status
            self._state.containers[name].error = error
            self.refresh()
