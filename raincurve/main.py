import typer

from raincurve import __version__

app = typer.Typer(
    name="raincurve",
    help="High-fidelity local production replicas in Docker.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def login() -> None:
    """Authenticate with Raincurve."""
    from raincurve.cli.cmd_login import run_login
    run_login()


@app.command()
def init() -> None:
    """Configure your LLM provider (OpenRouter / Claude / OpenAI)."""
    from raincurve.cli.cmd_init import run_init
    run_init()


@app.command()
def sandbox(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip trust confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output structured JSON (for CI/agents)"),
) -> None:
    """Analyze current folder and spin up a production sandbox."""
    from raincurve.cli.cmd_sandbox import run_sandbox
    run_sandbox(skip_trust=yes, json_output=json_output)


@app.command()
def chat(
    message: str = typer.Argument(None, help="One-shot message. Omit for interactive mode."),
    json_output: bool = typer.Option(False, "--json", help="Output structured JSON (for CI/agents)"),
) -> None:
    """Talk to your running sandbox. Ask it to test, probe, or explore."""
    from raincurve.cli.cmd_chat import run_chat
    run_chat(message, json_output=json_output)


@app.command()
def up() -> None:
    """Restore sandbox from the last snapshot."""
    from raincurve.cli.cmd_up import run_up
    run_up()


@app.command()
def down() -> None:
    """Snapshot and tear down the running sandbox."""
    from raincurve.cli.cmd_down import run_down
    run_down()


@app.command()
def doctor() -> None:
    """Check system readiness (Docker, disk, RAM, auth)."""
    from raincurve.cli.cmd_doctor import run_doctor
    run_doctor()


@app.command(name="help")
def help_cmd() -> None:
    """Show detailed help and examples."""
    from raincurve.cli.cmd_help import run_help
    run_help()


@app.callback(invoke_without_command=True)
def version_callback(
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
) -> None:
    if version:
        typer.echo(f"raincurve {__version__}")
        raise typer.Exit()
