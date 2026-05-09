from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DockerfileAnalysis(BaseModel):
    base_image: str
    stages: list[str] = Field(default_factory=list)
    exposed_ports: list[int] = Field(default_factory=list)
    cmd: str | None = None
    has_cache_mounts: bool = False


class ComposeService(BaseModel):
    name: str
    image: str | None = None
    build_context: str | None = None
    ports: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)


class ComposeAnalysis(BaseModel):
    services: list[ComposeService] = Field(default_factory=list)
    has_build_directives: bool = False
    uses_env_file: bool = False


class EnvVarInfo(BaseModel):
    name: str
    source_file: str
    example_value: str | None = None
    purpose: Literal[
        "database_url",
        "api_key",
        "secret",
        "service_url",
        "port",
        "flag",
        "other",
    ] = "other"
    service: str | None = None


class ServiceRecipe(BaseModel):
    name: str
    image: str
    memory: str = "512m"
    cpus: float = 0.5
    environment: dict[str, str] = Field(default_factory=dict)
    cmd_args: str = ""
    healthcheck: str | None = None
    env_wiring: dict[str, str] = Field(default_factory=dict)
    can_disable: bool = False
    needs_agent_research: bool = False


class RepoBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    language: str
    language_version: str | None = None
    framework: str | None = None
    framework_version: str | None = None
    package_manager: str | None = None

    has_dockerfile: bool = False
    dockerfile_path: str | None = None
    dockerfile_analysis: DockerfileAnalysis | None = None

    has_compose: bool = False
    compose_path: str | None = None
    compose_analysis: ComposeAnalysis | None = None

    has_dockerignore: bool = False

    app_port: int | None = None
    start_command: str | None = None
    build_command: str | None = None

    env_vars: list[EnvVarInfo] = Field(default_factory=list)

    database_type: str | None = None
    database_url_pattern: str | None = None
    migration_tool: str | None = None
    migration_command: str | None = None
    seed_command: str | None = None

    detected_services: list[ServiceRecipe] = Field(default_factory=list)

    file_tree: str = ""
    key_file_contents: dict[str, str] = Field(default_factory=dict)
