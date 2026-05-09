from __future__ import annotations

from raincurve.models.code_context import (
    APIRoute,
    AuthFlow,
    CodeContext,
    DatabaseInfo,
    EnvVarMapping,
    ProjectDocs,
    SDKUsage,
)


class TestCodeContext:
    def test_minimal_creation(self):
        ctx = CodeContext(
            project_name="test",
            project_dir="/tmp/test",
            language="python",
        )
        assert ctx.project_name == "test"
        assert ctx.sdk_usages == []
        assert ctx.env_vars == []
        assert ctx.database is None

    def test_full_creation(self):
        ctx = CodeContext(
            project_name="myapp",
            project_dir="/tmp/myapp",
            language="typescript",
            framework="nestjs",
            package_manager="yarn",
            docs=ProjectDocs(readme="# MyApp", claude_md="## Build\nnpm start"),
            sdk_usages=[
                SDKUsage(
                    service_name="stripe",
                    sdk_package="stripe",
                    init_file="src/billing/stripe.ts",
                    init_code_snippet='const stripe = new Stripe(process.env.STRIPE_SECRET_KEY)',
                    base_url_env_var="STRIPE_API_BASE",
                    api_key_env_var="STRIPE_SECRET_KEY",
                    endpoints_called=["POST /v1/customers", "POST /v1/charges"],
                    patching_strategy="set STRIPE_API_BASE env var",
                    files_using_sdk=["src/billing/stripe.ts", "src/api/checkout.ts"],
                ),
            ],
            env_vars=[
                EnvVarMapping(
                    name="DATABASE_URL",
                    source_files=["src/config/db.ts"],
                    purpose="PostgreSQL connection",
                    required=True,
                ),
            ],
            auth_flow=AuthFlow(
                provider="custom-jwt",
                login_endpoint="POST /api/auth/login",
                login_body_schema={"email": "string", "password": "string"},
                token_type="jwt",
                default_credentials={"email": "admin@test.com", "password": "admin"},
            ),
            api_routes=[
                APIRoute(method="GET", path="/api/users", handler_file="src/routes/users.ts"),
                APIRoute(method="POST", path="/api/users", handler_file="src/routes/users.ts"),
            ],
            database=DatabaseInfo(
                db_type="postgresql",
                connection_env_var="DATABASE_URL",
                migration_tool="prisma",
                migration_command="npx prisma migrate deploy",
                business_domain="e-commerce",
            ),
            app_description="An e-commerce platform for selling products online",
            core_entities=["Product", "Order", "Customer", "Cart"],
            core_user_flows=[
                "Sign up → Browse products → Add to cart → Checkout → Payment",
            ],
        )

        assert len(ctx.sdk_usages) == 1
        assert ctx.sdk_usages[0].service_name == "stripe"
        assert ctx.sdk_usages[0].base_url_env_var == "STRIPE_API_BASE"
        assert ctx.auth_flow.provider == "custom-jwt"
        assert ctx.database.db_type == "postgresql"
        assert len(ctx.core_entities) == 4

    def test_json_roundtrip(self):
        ctx = CodeContext(
            project_name="test",
            project_dir="/tmp/test",
            language="python",
            sdk_usages=[
                SDKUsage(
                    service_name="stripe",
                    sdk_package="stripe",
                    init_file="app/stripe.py",
                ),
            ],
            app_description="A test app",
        )
        json_str = ctx.model_dump_json()
        restored = CodeContext.model_validate_json(json_str)
        assert restored.project_name == "test"
        assert len(restored.sdk_usages) == 1
        assert restored.sdk_usages[0].service_name == "stripe"

    def test_project_docs(self):
        docs = ProjectDocs(
            readme="# MyApp\nA cool app",
            claude_md="## Commands\nnpm start",
        )
        assert docs.readme is not None
        assert docs.contributing is None
        assert docs.custom_docs == {}


class TestCodeAnalysisAgentVerifyDone:
    def test_verify_catches_empty_app_description(self):
        from raincurve.agents.code_analysis_agent import CodeAnalysisAgent

        agent = CodeAnalysisAgent(
            project_dir="/tmp/test",
            project_name="test",
        )
        result = agent._verify_done({
            "project_name": "test",
            "project_dir": "/tmp/test",
            "language": "python",
            "sdk_usages": [],
            "env_vars": [{"name": "DB_URL"}],
            "app_description": "",
        })
        assert result is not None
        assert "app_description" in result

    def test_verify_catches_missing_service_analysis(self):
        from raincurve.agents.code_analysis_agent import CodeAnalysisAgent
        from raincurve.stubs.detector import DetectionResult, ExternalService

        detection = DetectionResult()
        detection.detected_services = [
            ExternalService(
                name="stripe", env_vars=["STRIPE_KEY"],
                mock_port=0, base_url_env="", description="test",
            ),
        ]

        agent = CodeAnalysisAgent(
            project_dir="/tmp/test",
            project_name="test",
            detection_result=detection,
        )
        result = agent._verify_done({
            "project_name": "test",
            "project_dir": "/tmp/test",
            "language": "python",
            "sdk_usages": [],
            "env_vars": [{"name": "DB_URL"}],
            "app_description": "A test app",
        })
        assert result is not None
        assert "stripe" in result

    def test_verify_passes_when_complete(self):
        from raincurve.agents.code_analysis_agent import CodeAnalysisAgent

        agent = CodeAnalysisAgent(
            project_dir="/tmp/test",
            project_name="test",
        )
        result = agent._verify_done({
            "project_name": "test",
            "project_dir": "/tmp/test",
            "language": "python",
            "sdk_usages": [],
            "env_vars": [{"name": "DB_URL"}],
            "app_description": "A test application",
        })
        assert result is None
