from __future__ import annotations

from rich.prompt import Confirm, Prompt
from rich.table import Table

from raincurve.config.global_config import load_global_config, save_global_config
from raincurve.models.build_plan import BuildPlan, EnvVarSpec
from raincurve.ui.console import console, rc_print


def trust_check(project_dir: str) -> bool:
    cfg = load_global_config()
    if project_dir in cfg.trusted_paths:
        return True

    rc_print(f"\n  Folder: [bold]{project_dir}[/bold]")
    trusted = Confirm.ask("  Trust this folder?", default=True)
    if trusted:
        cfg.trusted_paths.append(project_dir)
        save_global_config(cfg)
    return trusted


def present_plan(plan: BuildPlan) -> bool:
    console.print()
    rc_print(f"[bold]Build Plan:[/bold] {plan.project_name}")
    if plan.rationale:
        rc_print(f"  {plan.rationale}", style="rc.dim")
    console.print()

    table = Table(show_header=True, header_style="bold cyan", padding=(0, 2))
    table.add_column("Service", style="bold")
    table.add_column("Type")
    table.add_column("Port")
    table.add_column("Memory")
    table.add_column("CPUs")

    for svc in plan.services:
        svc_type = "image" if svc.image else "build"
        port = str(svc.port) if svc.port else "-"
        table.add_row(
            svc.name,
            svc_type,
            port,
            svc.resource_limits.memory,
            str(svc.resource_limits.cpus),
        )

    console.print(table)

    if plan.warnings:
        for w in plan.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {w}")
        console.print()

    return Confirm.ask("\n  Proceed with this plan?", default=True)


def collect_env_vars(env_vars: list[EnvVarSpec]) -> dict[str, str]:
    values: dict[str, str] = {}
    required = [v for v in env_vars if v.required and v.inferred_value is None]
    optional = [v for v in env_vars if not v.required and v.inferred_value is None]
    inferred = [v for v in env_vars if v.inferred_value is not None]

    for v in inferred:
        values[v.name] = v.inferred_value  # type: ignore[assignment]

    if required:
        rc_print("\n[bold]Required environment variables:[/bold]")
        for v in required:
            desc = f" ({v.description})" if v.description else ""
            values[v.name] = Prompt.ask(
                f"  {v.name}{desc}",
                password=v.sensitive,
            )

    if optional:
        rc_print("\n[bold]Optional environment variables[/bold] (press Enter to skip):")
        for v in optional:
            desc = f" ({v.description})" if v.description else ""
            val = Prompt.ask(
                f"  {v.name}{desc}",
                default="",
                password=v.sensitive,
            )
            if val:
                values[v.name] = val

    return values
