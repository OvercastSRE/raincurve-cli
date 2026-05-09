from __future__ import annotations

from pydantic import BaseModel, Field


class SDKUsage(BaseModel):
    """One external service SDK as used in the codebase."""

    service_name: str
    sdk_package: str
    init_file: str
    init_code_snippet: str = ""
    base_url_env_var: str | None = None
    base_url_hardcoded: str | None = None
    api_key_env_var: str | None = None
    endpoints_called: list[str] = Field(default_factory=list)
    patching_strategy: str = ""
    files_using_sdk: list[str] = Field(default_factory=list)


class EnvVarMapping(BaseModel):
    """An environment variable the app reads."""

    name: str
    source_files: list[str] = Field(default_factory=list)
    default_value: str | None = None
    purpose: str = ""
    required: bool = False


class AuthFlow(BaseModel):
    """How the app handles authentication."""

    provider: str = ""
    middleware_file: str | None = None
    login_endpoint: str | None = None
    login_body_schema: dict | None = None
    token_type: str = ""
    default_credentials: dict | None = None
    bypass_strategy: str = ""


class APIRoute(BaseModel):
    """An API route exposed by the application."""

    method: str
    path: str
    handler_file: str = ""
    requires_auth: bool = False
    description: str = ""


class DatabaseInfo(BaseModel):
    """Database connection and schema information."""

    db_type: str
    connection_env_var: str = ""
    connection_file: str | None = None
    migration_tool: str | None = None
    migration_command: str | None = None
    seed_command: str | None = None
    schema_summary: str = ""
    business_domain: str = ""


class ProjectDocs(BaseModel):
    """Content from project documentation files (Step 0)."""

    readme: str | None = None
    claude_md: str | None = None
    contributing: str | None = None
    agents_md: str | None = None
    cursor_rules: str | None = None
    custom_docs: dict[str, str] = Field(default_factory=dict)


class CodeContext(BaseModel):
    """Complete structured understanding of a codebase.

    Produced by CodeAnalysisAgent (Step 1).
    Consumed by ALL downstream agents.
    Persisted to .raincurve/code_context.json.
    """

    project_name: str
    project_dir: str
    language: str
    framework: str | None = None
    package_manager: str | None = None

    docs: ProjectDocs = Field(default_factory=ProjectDocs)

    sdk_usages: list[SDKUsage] = Field(default_factory=list)
    env_vars: list[EnvVarMapping] = Field(default_factory=list)
    auth_flow: AuthFlow = Field(default_factory=AuthFlow)
    api_routes: list[APIRoute] = Field(default_factory=list)
    database: DatabaseInfo | None = None

    entry_point: str | None = None
    start_command: str | None = None
    build_command: str | None = None
    dockerfile_path: str | None = None
    compose_path: str | None = None

    app_description: str = ""
    core_entities: list[str] = Field(default_factory=list)
    core_user_flows: list[str] = Field(default_factory=list)
