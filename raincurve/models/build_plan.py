from __future__ import annotations

from pydantic import BaseModel, Field


class ResourceLimits(BaseModel):
    memory: str = "512m"
    cpus: float = 0.5


class EnvVarSpec(BaseModel):
    name: str
    required: bool = False
    description: str = ""
    inferred_value: str | None = None
    sensitive: bool = False


class ServiceSpec(BaseModel):
    name: str
    image: str | None = None
    dockerfile_path: str | None = None
    build_context: str | None = None
    build_args: dict[str, str] = Field(default_factory=dict)
    port: int | None = None
    host_port: int | None = None
    env_vars: list[EnvVarSpec] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    healthcheck_cmd: str | None = None
    seed_commands: list[str] = Field(default_factory=list)
    volumes: dict[str, str] = Field(default_factory=dict)


class BuildPlan(BaseModel):
    project_name: str
    services: list[ServiceSpec]
    network_name: str = ""
    seed_strategy: str = "none"
    confidence: float = 0.0
    rationale: str = ""
    warnings: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        if not self.network_name:
            self.network_name = f"rc-{self.project_name}"
