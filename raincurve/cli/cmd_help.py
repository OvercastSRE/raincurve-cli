from raincurve.ui.console import console


HELP_TEXT = """\
[bold cyan]raincurve[/bold cyan] - High-fidelity local production replicas in Docker

[bold]Commands:[/bold]

  [cyan]raincurve login[/cyan]      Authenticate with Raincurve
  [cyan]raincurve init[/cyan]       Configure your LLM provider (OpenRouter / Claude / OpenAI)
  [cyan]raincurve sandbox[/cyan]    Analyze current folder and spin up a production sandbox
  [cyan]raincurve up[/cyan]         Restore a sandbox from the last snapshot
  [cyan]raincurve down[/cyan]       Snapshot and tear down the running sandbox
  [cyan]raincurve doctor[/cyan]     Check system readiness (Docker, disk, RAM, auth)
  [cyan]raincurve help[/cyan]       Show this help

[bold]Getting started:[/bold]

  1. [dim]raincurve login[/dim]           # auth with Raincurve
  2. [dim]raincurve init[/dim]            # set up Claude or OpenAI
  3. [dim]cd your-project/[/dim]
  4. [dim]raincurve sandbox[/dim]         # builds and runs everything in Docker
  5. [dim]raincurve down[/dim]            # snapshots state, tears down containers
  6. [dim]raincurve up[/dim]              # restores from snapshot instantly

[bold]How it works:[/bold]

  raincurve sandbox reads your project, uses an LLM to figure out how to build
  and run it, then spins up Docker containers for every service - app, database,
  cache, workers. It watches for code changes and auto-rebuilds.

  If a build fails, the LLM reads the error, patches the Dockerfile, and retries.

  All config is stored in [dim].raincurve/[/dim] inside your project. Gitignore the
  snapshots and run state; keep config.json if you want repeatable builds.

[bold]Links:[/bold]

  Docs:    https://raincurve.dev/docs
  Issues:  https://github.com/raincurve/raincurve/issues
"""


def run_help() -> None:
    console.print(HELP_TEXT)
