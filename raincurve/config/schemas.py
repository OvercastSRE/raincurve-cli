from __future__ import annotations

from pydantic import BaseModel, Field

GLOBAL_MEMORY_BUDGET_MB = 14_336
GLOBAL_DISK_BUDGET_GB = 240
MAX_CONTAINERS = 20


class RaincurveAuth(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: str | None = None
    email: str | None = None


class LLMConfig(BaseModel):
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None


class DefaultLimits(BaseModel):
    memory_budget_mb: int = GLOBAL_MEMORY_BUDGET_MB
    disk_budget_gb: int = GLOBAL_DISK_BUDGET_GB
    max_containers: int = MAX_CONTAINERS


class GlobalConfig(BaseModel):
    version: int = 1
    auth: RaincurveAuth = Field(default_factory=RaincurveAuth)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    defaults: DefaultLimits = Field(default_factory=DefaultLimits)
    trusted_paths: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    version: int = 1
    project_name: str = ""
    network_name: str = ""
    services: list[dict] = Field(default_factory=list)
    seed_strategy: str = "none"
    env_var_defaults: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None
    last_sandbox_at: str | None = None
