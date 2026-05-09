from __future__ import annotations

from raincurve.config.schemas import GLOBAL_MEMORY_BUDGET_MB
from raincurve.models.build_plan import ResourceLimits, ServiceSpec


def _parse_memory_mb(mem: str) -> int:
    mem = mem.strip().lower()
    if mem.endswith("g"):
        return int(float(mem[:-1]) * 1024)
    if mem.endswith("m"):
        return int(float(mem[:-1]))
    return int(mem)


def validate_limits(services: list[ServiceSpec], budget_mb: int = GLOBAL_MEMORY_BUDGET_MB) -> list[str]:
    warnings: list[str] = []
    total = sum(_parse_memory_mb(s.resource_limits.memory) for s in services)
    pct = (total / budget_mb) * 100

    if pct > 100:
        warnings.append(
            f"Total memory ({total}MB) exceeds budget ({budget_mb}MB). "
            "Reduce per-service limits or increase budget."
        )
    elif pct > 80:
        warnings.append(
            f"Total memory ({total}MB) is {pct:.0f}% of budget ({budget_mb}MB)."
        )

    for s in services:
        mb = _parse_memory_mb(s.resource_limits.memory)
        if mb > 4096:
            warnings.append(
                f"Service '{s.name}' requests {s.resource_limits.memory} — "
                "consider reducing to ≤4g unless required."
            )

    return warnings


DEFAULT_LIMITS: dict[str, ResourceLimits] = {
    "stateless": ResourceLimits(memory="512m", cpus=0.5),
    "database": ResourceLimits(memory="1g", cpus=1.0),
    "cache": ResourceLimits(memory="256m", cpus=0.25),
    "worker": ResourceLimits(memory="512m", cpus=0.5),
}
