from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".raincurve", "vendor",
}

SCAN_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".java", ".rs"}


@dataclass
class ExternalService:
    name: str
    env_vars: list[str]
    mock_port: int
    base_url_env: str
    description: str


KNOWN_SERVICES: list[ExternalService] = [
    ExternalService(
        name="supabase",
        env_vars=[
            "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE",
            "SUPABASE_SERVICE_ROLE_KEY", "NEXT_PUBLIC_SUPABASE_URL",
            "NEXT_PUBLIC_SUPABASE_ANON_KEY",
        ],
        mock_port=54321,
        base_url_env="SUPABASE_URL",
        description="Auth + Postgres + Storage + Realtime (full BaaS)",
    ),
    ExternalService(
        name="stripe",
        env_vars=[
            "STRIPE_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY",
            "STRIPE_WEBHOOK_SECRET", "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY",
        ],
        mock_port=12111,
        base_url_env="STRIPE_API_BASE",
        description="Payment processing (subscriptions, charges, webhooks)",
    ),
    ExternalService(
        name="openai",
        env_vars=["OPENAI_API_KEY", "OPENAI_BASE_URL"],
        mock_port=12112,
        base_url_env="OPENAI_BASE_URL",
        description="LLM / AI",
    ),
    ExternalService(
        name="anthropic",
        env_vars=["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"],
        mock_port=12113,
        base_url_env="ANTHROPIC_BASE_URL",
        description="LLM / AI",
    ),
    ExternalService(
        name="aws-s3",
        env_vars=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET", "S3_BUCKET", "AWS_ENDPOINT_URL"],
        mock_port=12116,
        base_url_env="AWS_ENDPOINT_URL",
        description="Object storage (S3-compatible)",
    ),
    ExternalService(
        name="aws-bedrock",
        env_vars=["AWS_BEDROCK_REGION", "AWS_ACCESS_KEY_ID"],
        mock_port=0,
        base_url_env="",
        description="AWS Bedrock LLM provider",
    ),
    ExternalService(
        name="firebase",
        env_vars=["FIREBASE_API_KEY", "FIREBASE_PROJECT_ID", "GOOGLE_APPLICATION_CREDENTIALS"],
        mock_port=12118,
        base_url_env="FIREBASE_AUTH_EMULATOR_HOST",
        description="Auth / database / messaging",
    ),
    ExternalService(
        name="posthog",
        env_vars=["NEXT_PUBLIC_POSTHOG_KEY", "POSTHOG_API_KEY", "POSTHOG_HOST", "NEXT_PUBLIC_POSTHOG_HOST"],
        mock_port=0,
        base_url_env="NEXT_PUBLIC_POSTHOG_HOST",
        description="Product analytics (can be disabled)",
    ),
    ExternalService(
        name="sendgrid",
        env_vars=["SENDGRID_API_KEY"],
        mock_port=12114,
        base_url_env="SENDGRID_API_BASE",
        description="Email delivery",
    ),
    ExternalService(
        name="twilio",
        env_vars=["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"],
        mock_port=12115,
        base_url_env="TWILIO_API_BASE",
        description="SMS / voice",
    ),
    ExternalService(
        name="google-maps",
        env_vars=["GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_KEY"],
        mock_port=12117,
        base_url_env="GOOGLE_MAPS_BASE_URL",
        description="Maps / geocoding",
    ),
    ExternalService(
        name="slack",
        env_vars=["SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL", "SLACK_API_TOKEN"],
        mock_port=12119,
        base_url_env="SLACK_API_BASE",
        description="Messaging / webhooks",
    ),
    ExternalService(
        name="github-api",
        env_vars=["GITHUB_TOKEN", "GITHUB_API_KEY"],
        mock_port=12120,
        base_url_env="GITHUB_API_URL",
        description="GitHub API",
    ),
    ExternalService(
        name="redis",
        env_vars=["REDIS_URL", "REDIS_HOST", "UPSTASH_REDIS_URL"],
        mock_port=0,
        base_url_env="REDIS_URL",
        description="In-memory cache / message broker",
    ),
    # --- Auth providers ---
    ExternalService(
        name="google-oauth",
        env_vars=[
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_ID",
            "NEXT_PUBLIC_GOOGLE_CLIENT_ID",
        ],
        mock_port=12121,
        base_url_env="GOOGLE_AUTH_BASE_URL",
        description="Google OAuth / Sign-In",
    ),
    ExternalService(
        name="auth0",
        env_vars=[
            "AUTH0_DOMAIN", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET",
            "AUTH0_ISSUER_BASE_URL", "AUTH0_BASE_URL", "AUTH0_AUDIENCE",
        ],
        mock_port=12122,
        base_url_env="AUTH0_ISSUER_BASE_URL",
        description="Auth0 identity platform (login, SSO, MFA)",
    ),
    ExternalService(
        name="clerk",
        env_vars=[
            "CLERK_SECRET_KEY", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
            "CLERK_PUBLISHABLE_KEY", "NEXT_PUBLIC_CLERK_SIGN_IN_URL",
        ],
        mock_port=12123,
        base_url_env="CLERK_API_URL",
        description="Clerk auth (sessions, user management, components)",
    ),
    ExternalService(
        name="okta",
        env_vars=["OKTA_DOMAIN", "OKTA_CLIENT_ID", "OKTA_CLIENT_SECRET", "OKTA_ISSUER"],
        mock_port=12124,
        base_url_env="OKTA_ISSUER",
        description="Okta identity / SSO",
    ),
    ExternalService(
        name="aws-cognito",
        env_vars=[
            "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "AWS_COGNITO_REGION",
            "NEXT_PUBLIC_COGNITO_USER_POOL_ID", "NEXT_PUBLIC_COGNITO_CLIENT_ID",
        ],
        mock_port=12125,
        base_url_env="COGNITO_ENDPOINT",
        description="AWS Cognito user pools / identity",
    ),
    # --- Email ---
    ExternalService(
        name="resend",
        env_vars=["RESEND_API_KEY"],
        mock_port=12126,
        base_url_env="RESEND_API_BASE",
        description="Email delivery (modern Resend API)",
    ),
    ExternalService(
        name="mailgun",
        env_vars=["MAILGUN_API_KEY", "MAILGUN_DOMAIN"],
        mock_port=12127,
        base_url_env="MAILGUN_API_BASE",
        description="Email delivery (Mailgun)",
    ),
    ExternalService(
        name="aws-ses",
        env_vars=["AWS_SES_REGION", "SES_SENDER", "AWS_SES_ACCESS_KEY"],
        mock_port=12128,
        base_url_env="AWS_SES_ENDPOINT",
        description="AWS Simple Email Service",
    ),
    ExternalService(
        name="postmark",
        env_vars=["POSTMARK_API_KEY", "POSTMARK_SERVER_TOKEN"],
        mock_port=12129,
        base_url_env="POSTMARK_API_BASE",
        description="Transactional email (Postmark)",
    ),
    # --- Webhooks / Realtime ---
    ExternalService(
        name="svix",
        env_vars=["SVIX_API_KEY", "SVIX_TOKEN"],
        mock_port=12130,
        base_url_env="SVIX_SERVER_URL",
        description="Webhook delivery / management",
    ),
    ExternalService(
        name="pusher",
        env_vars=[
            "PUSHER_APP_ID", "PUSHER_KEY", "PUSHER_SECRET",
            "NEXT_PUBLIC_PUSHER_KEY", "PUSHER_CLUSTER",
        ],
        mock_port=12131,
        base_url_env="PUSHER_HOST",
        description="Realtime channels (websockets)",
    ),
    ExternalService(
        name="ably",
        env_vars=["ABLY_API_KEY", "ABLY_KEY"],
        mock_port=12132,
        base_url_env="ABLY_REST_HOST",
        description="Realtime messaging (Ably)",
    ),
    ExternalService(
        name="discord",
        env_vars=["DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL", "DISCORD_CLIENT_ID"],
        mock_port=12133,
        base_url_env="DISCORD_API_BASE",
        description="Discord bot / webhooks",
    ),
    # --- Search / Vector DBs ---
    ExternalService(
        name="algolia",
        env_vars=["ALGOLIA_APP_ID", "ALGOLIA_API_KEY", "ALGOLIA_ADMIN_KEY", "NEXT_PUBLIC_ALGOLIA_APP_ID"],
        mock_port=12134,
        base_url_env="ALGOLIA_API_BASE",
        description="Search-as-a-service (Algolia)",
    ),
    ExternalService(
        name="elasticsearch",
        env_vars=["ELASTICSEARCH_URL", "ELASTIC_URL", "ES_HOST"],
        mock_port=0,
        base_url_env="ELASTICSEARCH_URL",
        description="Full-text search (Elasticsearch)",
    ),
    ExternalService(
        name="pinecone",
        env_vars=["PINECONE_API_KEY", "PINECONE_ENVIRONMENT", "PINECONE_INDEX"],
        mock_port=12135,
        base_url_env="PINECONE_BASE_URL",
        description="Vector database (Pinecone)",
    ),
    ExternalService(
        name="weaviate",
        env_vars=["WEAVIATE_URL", "WEAVIATE_API_KEY"],
        mock_port=0,
        base_url_env="WEAVIATE_URL",
        description="Vector database (Weaviate)",
    ),
    ExternalService(
        name="qdrant",
        env_vars=["QDRANT_URL", "QDRANT_API_KEY", "QDRANT_HOST"],
        mock_port=0,
        base_url_env="QDRANT_URL",
        description="Vector database (Qdrant)",
    ),
    ExternalService(
        name="meilisearch",
        env_vars=["MEILISEARCH_URL", "MEILISEARCH_HOST", "MEILISEARCH_API_KEY", "MEILI_MASTER_KEY"],
        mock_port=0,
        base_url_env="MEILISEARCH_URL",
        description="Full-text search (Meilisearch)",
    ),
    # --- Media / Files ---
    ExternalService(
        name="cloudinary",
        env_vars=["CLOUDINARY_URL", "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"],
        mock_port=12136,
        base_url_env="CLOUDINARY_URL",
        description="Image/video management and CDN",
    ),
    ExternalService(
        name="uploadthing",
        env_vars=["UPLOADTHING_SECRET", "UPLOADTHING_APP_ID"],
        mock_port=12137,
        base_url_env="UPLOADTHING_API_BASE",
        description="File upload service (UploadThing)",
    ),
    # --- Monitoring / Error tracking ---
    ExternalService(
        name="sentry",
        env_vars=["SENTRY_DSN", "NEXT_PUBLIC_SENTRY_DSN", "SENTRY_AUTH_TOKEN"],
        mock_port=0,
        base_url_env="SENTRY_DSN",
        description="Error tracking (can be disabled)",
    ),
    ExternalService(
        name="datadog",
        env_vars=["DD_API_KEY", "DD_APP_KEY", "DATADOG_API_KEY"],
        mock_port=0,
        base_url_env="DD_AGENT_HOST",
        description="Monitoring / APM (can be disabled)",
    ),
    # --- Databases (cloud) ---
    ExternalService(
        name="mongodb",
        env_vars=["MONGODB_URI", "MONGO_URL", "MONGODB_URL", "MONGO_URI"],
        mock_port=0,
        base_url_env="MONGODB_URI",
        description="MongoDB (run local mongod or mongo:7 container)",
    ),
    ExternalService(
        name="neon",
        env_vars=["NEON_DATABASE_URL", "NEON_API_KEY"],
        mock_port=0,
        base_url_env="NEON_DATABASE_URL",
        description="Serverless Postgres (Neon) — replace with local Postgres",
    ),
    ExternalService(
        name="planetscale",
        env_vars=["PLANETSCALE_DATABASE_URL", "PLANETSCALE_TOKEN"],
        mock_port=0,
        base_url_env="PLANETSCALE_DATABASE_URL",
        description="Serverless MySQL (PlanetScale) — replace with local MySQL",
    ),
    ExternalService(
        name="turso",
        env_vars=["TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"],
        mock_port=0,
        base_url_env="TURSO_DATABASE_URL",
        description="Edge SQLite (Turso) — replace with local libSQL/SQLite",
    ),
    # --- Messaging / Queues ---
    ExternalService(
        name="rabbitmq",
        env_vars=["RABBITMQ_URL", "AMQP_URL", "CLOUDAMQP_URL"],
        mock_port=0,
        base_url_env="RABBITMQ_URL",
        description="Message broker (RabbitMQ)",
    ),
    ExternalService(
        name="kafka",
        env_vars=["KAFKA_BROKERS", "KAFKA_URL", "UPSTASH_KAFKA_REST_URL"],
        mock_port=0,
        base_url_env="KAFKA_BROKERS",
        description="Event streaming (Kafka)",
    ),
    ExternalService(
        name="aws-sqs",
        env_vars=["SQS_QUEUE_URL", "AWS_SQS_QUEUE_URL"],
        mock_port=0,
        base_url_env="AWS_ENDPOINT_URL",
        description="AWS SQS message queue",
    ),
    # --- Payments (additional) ---
    ExternalService(
        name="paypal",
        env_vars=["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID"],
        mock_port=12138,
        base_url_env="PAYPAL_API_BASE",
        description="PayPal payments",
    ),
    ExternalService(
        name="lemonsqueezy",
        env_vars=["LEMONSQUEEZY_API_KEY", "LEMON_SQUEEZY_API_KEY", "LEMONSQUEEZY_WEBHOOK_SECRET"],
        mock_port=12139,
        base_url_env="LEMONSQUEEZY_API_BASE",
        description="Lemon Squeezy payments / subscriptions",
    ),
    # --- Maps (additional) ---
    ExternalService(
        name="mapbox",
        env_vars=["MAPBOX_ACCESS_TOKEN", "NEXT_PUBLIC_MAPBOX_TOKEN", "MAPBOX_TOKEN"],
        mock_port=12140,
        base_url_env="MAPBOX_API_BASE",
        description="Mapbox maps / geocoding",
    ),
    # --- Feature flags ---
    ExternalService(
        name="launchdarkly",
        env_vars=["LAUNCHDARKLY_SDK_KEY", "LD_SDK_KEY", "LAUNCHDARKLY_CLIENT_ID"],
        mock_port=0,
        base_url_env="LAUNCHDARKLY_BASE_URI",
        description="Feature flags (can be disabled)",
    ),
    # --- Other common APIs ---
    ExternalService(
        name="plaid",
        env_vars=["PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV"],
        mock_port=12141,
        base_url_env="PLAID_BASE_URL",
        description="Financial data / banking (Plaid)",
    ),
    ExternalService(
        name="vercel-kv",
        env_vars=["KV_REST_API_URL", "KV_REST_API_TOKEN", "KV_URL"],
        mock_port=0,
        base_url_env="KV_REST_API_URL",
        description="Vercel KV (serverless Redis) — replace with local Redis",
    ),
    ExternalService(
        name="vercel-blob",
        env_vars=["BLOB_READ_WRITE_TOKEN"],
        mock_port=12142,
        base_url_env="BLOB_API_BASE",
        description="Vercel Blob storage",
    ),
]

# Patterns to detect SDK imports
IMPORT_PATTERNS: dict[str, list[str]] = {
    "supabase": [r"@supabase/supabase-js", r"from\s+supabase", r"createClient.*supabase", r"createBrowserClient", r"createServerClient"],
    "stripe": [r"import\s+stripe", r"require\(['\"]stripe['\"]", r"from\s+stripe"],
    "openai": [r"import\s+openai", r"require\(['\"]openai['\"]", r"from\s+openai"],
    "anthropic": [r"import\s+anthropic", r"require\(['\"]@?anthropic", r"from\s+anthropic", r"@anthropic-ai/sdk"],
    "aws-s3": [r"import\s+boto3", r"aws-sdk", r"@aws-sdk/client-s3", r"from\s+boto3"],
    "aws-bedrock": [r"@aws-sdk/client-bedrock", r"bedrock-runtime", r"BedrockRuntime"],
    "firebase": [r"firebase-admin", r"firebase/app", r"from\s+firebase_admin"],
    "posthog": [r"posthog-js", r"posthog-node", r"from\s+posthog", r"posthog\.capture"],
    "sendgrid": [r"import\s+sendgrid", r"@sendgrid/mail", r"from\s+sendgrid"],
    "twilio": [r"import\s+twilio", r"require\(['\"]twilio['\"]", r"from\s+twilio"],
    "google-maps": [r"@googlemaps", r"googlemaps", r"google.maps"],
    "slack": [r"@slack/web-api", r"slack_sdk", r"from\s+slack"],
    "github-api": [r"@octokit", r"from\s+github", r"PyGithub"],
    "redis": [r"import\s+redis", r"from\s+redis", r"ioredis", r"@upstash/redis"],
    # Auth providers
    "google-oauth": [r"GoogleProvider", r"google-auth-library", r"googleapis", r"passport-google", r"next-auth.*google", r"GoogleOAuthProvider"],
    "auth0": [r"@auth0/", r"auth0-js", r"from\s+auth0", r"nextjs-auth0", r"passport-auth0"],
    "clerk": [r"@clerk/", r"clerkMiddleware", r"ClerkProvider", r"useUser.*clerk"],
    "okta": [r"@okta/", r"okta-auth-js", r"passport-okta", r"OktaAuth"],
    "aws-cognito": [r"amazon-cognito-identity", r"@aws-amplify/auth", r"CognitoUser", r"cognito-express"],
    # Email
    "resend": [r"from\s+resend", r"import\s+resend", r"require\(['\"]resend['\"]", r"Resend\("],
    "mailgun": [r"mailgun[.-]js", r"from\s+mailgun", r"import\s+mailgun"],
    "aws-ses": [r"@aws-sdk/client-ses", r"SESClient", r"from\s+boto3.*ses"],
    "postmark": [r"postmark", r"ServerClient.*postmark"],
    # Webhooks / Realtime
    "svix": [r"from\s+svix", r"import\s+svix", r"require\(['\"]svix['\"]", r"Svix\("],
    "pusher": [r"require\(['\"]pusher['\"]", r"from\s+pusher", r"import\s+Pusher", r"pusher-js"],
    "ably": [r"from\s+ably", r"import\s+ably", r"require\(['\"]ably['\"]", r"Ably\.Realtime"],
    "discord": [r"discord\.js", r"discord\.py", r"from\s+discord", r"import\s+discord"],
    # Search / Vector DBs
    "algolia": [r"algoliasearch", r"from\s+algoliasearch", r"instantsearch", r"react-instantsearch"],
    "elasticsearch": [r"@elastic/elasticsearch", r"from\s+elasticsearch", r"import\s+elasticsearch"],
    "pinecone": [r"@pinecone-database", r"from\s+pinecone", r"import\s+pinecone", r"Pinecone\("],
    "weaviate": [r"weaviate-client", r"weaviate-ts-client", r"from\s+weaviate"],
    "qdrant": [r"qdrant-client", r"qdrant_client", r"from\s+qdrant_client", r"QdrantClient"],
    "meilisearch": [r"meilisearch", r"from\s+meilisearch", r"MeiliSearch"],
    # Media / Files
    "cloudinary": [r"cloudinary", r"from\s+cloudinary", r"import\s+cloudinary"],
    "uploadthing": [r"uploadthing", r"@uploadthing/", r"createUploadthing"],
    # Monitoring
    "sentry": [r"@sentry/", r"import\s+sentry_sdk", r"from\s+sentry_sdk", r"Sentry\.init"],
    "datadog": [r"dd-trace", r"ddtrace", r"from\s+datadog", r"import\s+datadog"],
    # Databases (cloud)
    "mongodb": [r"from\s+pymongo", r"import\s+pymongo", r"mongoose", r"mongodb", r"MongoClient"],
    "neon": [r"@neondatabase/serverless", r"neon-serverless"],
    "planetscale": [r"@planetscale/database", r"planetscale-driver"],
    "turso": [r"@libsql/client", r"libsql_client", r"createClient.*turso"],
    # Messaging / Queues
    "rabbitmq": [r"amqplib", r"from\s+pika", r"import\s+pika", r"amqp"],
    "kafka": [r"kafkajs", r"kafka-python", r"from\s+kafka", r"confluent_kafka"],
    "aws-sqs": [r"@aws-sdk/client-sqs", r"SQSClient", r"boto3.*sqs"],
    # Payments (additional)
    "paypal": [r"@paypal/", r"paypal-rest-sdk", r"from\s+paypalrestsdk"],
    "lemonsqueezy": [r"@lemonsqueezy/", r"lemonsqueezy\.js"],
    # Maps
    "mapbox": [r"mapbox-gl", r"react-map-gl", r"@mapbox/", r"mapboxgl"],
    # Feature flags
    "launchdarkly": [r"launchdarkly", r"ldclient", r"@launchdarkly/"],
    # Other
    "plaid": [r"plaid-node", r"from\s+plaid", r"PlaidClient", r"plaid-python"],
    "vercel-kv": [r"@vercel/kv"],
    "vercel-blob": [r"@vercel/blob"],
}


@dataclass
class DetectionResult:
    detected_services: list[ExternalService] = field(default_factory=list)
    env_vars_found: dict[str, str] = field(default_factory=dict)
    import_hits: dict[str, list[str]] = field(default_factory=dict)


def detect_external_services(project_dir: str | Path) -> DetectionResult:
    project_dir = Path(project_dir)
    result = DetectionResult()
    seen_services: set[str] = set()

    # Scan env files for known env vars
    for env_file in ["env.example", ".env.example", ".env.sample", ".env.template", ".env"]:
        path = project_dir / env_file
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                for svc in KNOWN_SERVICES:
                    for var in svc.env_vars:
                        if var in content:
                            result.env_vars_found[var] = svc.name
                            seen_services.add(svc.name)
            except (PermissionError, OSError):
                pass

    # Scan source files for SDK imports
    for src_file in _walk_source_files(project_dir):
        try:
            content = src_file.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            continue

        for svc_name, patterns in IMPORT_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, content):
                    if svc_name not in result.import_hits:
                        result.import_hits[svc_name] = []
                    rel = str(src_file.relative_to(project_dir))
                    if rel not in result.import_hits[svc_name]:
                        result.import_hits[svc_name].append(rel)
                    seen_services.add(svc_name)
                    break

    # Build final service list
    svc_map = {s.name: s for s in KNOWN_SERVICES}
    for name in seen_services:
        if name in svc_map:
            result.detected_services.append(svc_map[name])

    return result


def _walk_source_files(root: Path):
    for path in root.rglob("*"):
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue
        if path.suffix not in SCAN_EXTENSIONS:
            continue
        try:
            if path.is_file():
                yield path
        except OSError:
            continue
