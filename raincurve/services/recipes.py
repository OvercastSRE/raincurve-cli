from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServiceRecipe:
    image: str
    memory: str  # e.g. "256m"
    cpus: float
    environment: dict[str, str]
    cmd_args: str  # extra args after image name
    healthcheck: str  # command to check health
    env_wiring: dict[str, str]  # env vars to set on the app container, {name} is container name
    internal_port: int = 0  # primary port the service listens on inside the container
    shm_size: str | None = None  # for postgres
    ports: list[str] | None = None  # host port mappings if needed


# ---------------------------------------------------------------------------
# Pre-baked recipes — keyed by canonical service name
# ---------------------------------------------------------------------------

_RECIPES: dict[str, ServiceRecipe] = {
    # ------------------------------------------------------------------
    # Databases
    # ------------------------------------------------------------------
    "postgres": ServiceRecipe(
        image="postgres:16-alpine",
        memory="256m",
        cpus=0.5,
        internal_port=5432,
        shm_size="64m",
        environment={
            "POSTGRES_DB": "app",
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": "postgres",
        },
        cmd_args=(
            "-c shared_buffers=32MB "
            "-c work_mem=4MB "
            "-c maintenance_work_mem=16MB "
            "-c effective_cache_size=128MB "
            "-c max_connections=20 "
            "-c wal_level=minimal "
            "-c max_wal_senders=0 "
            "-c fsync=off "
            "-c synchronous_commit=off "
            "-c full_page_writes=off "
            "-c checkpoint_timeout=30min "
            "-c max_wal_size=256MB"
        ),
        healthcheck="pg_isready -U postgres",
        env_wiring={
            "DATABASE_URL": "postgresql://postgres:postgres@{name}:5432/app",
            "POSTGRES_HOST": "{name}",
            "PGHOST": "{name}",
        },
    ),
    "mysql": ServiceRecipe(
        image="mysql:8.0",
        memory="256m",
        cpus=0.5,
        internal_port=3306,
        environment={
            "MYSQL_ROOT_PASSWORD": "root",
            "MYSQL_DATABASE": "app",
            "MYSQL_USER": "app",
            "MYSQL_PASSWORD": "app",
        },
        cmd_args=(
            "--innodb-buffer-pool-size=32M "
            "--innodb-log-file-size=16M "
            "--innodb-flush-log-at-trx-commit=0 "
            "--innodb-flush-method=nosync "
            "--max-connections=20 "
            "--performance-schema=OFF "
            "--skip-log-bin"
        ),
        healthcheck="mysqladmin ping -h 127.0.0.1 -u root -proot",
        env_wiring={
            "DATABASE_URL": "mysql://app:app@{name}:3306/app",
            "MYSQL_HOST": "{name}",
        },
    ),
    "redis": ServiceRecipe(
        image="redis:7-alpine",
        memory="64m",
        cpus=0.25,
        internal_port=6379,
        environment={},
        cmd_args=(
            '--maxmemory 32mb '
            '--maxmemory-policy allkeys-lru '
            '--save "" '
            '--appendonly no'
        ),
        healthcheck="redis-cli ping",
        env_wiring={
            "REDIS_URL": "redis://{name}:6379/0",
            "REDIS_HOST": "{name}",
        },
    ),
    "mongodb": ServiceRecipe(
        image="mongo:7",
        memory="256m",
        cpus=0.5,
        internal_port=27017,
        environment={},
        cmd_args="--wiredTigerCacheSizeGB 0.1 --nojournal",
        healthcheck='mongosh --eval "db.runCommand({ping:1})" --quiet',
        env_wiring={
            "MONGODB_URI": "mongodb://{name}:27017/app",
            "MONGO_URL": "mongodb://{name}:27017/app",
        },
    ),
    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------
    "stripe": ServiceRecipe(
        image="stripe/stripe-mock:latest",
        memory="64m",
        cpus=0.25,
        internal_port=12111,
        environment={},
        cmd_args="-http-port 12111",
        healthcheck="wget -qO- http://127.0.0.1:12111/v1/charges || exit 1",
        env_wiring={
            "STRIPE_API_KEY": "sk_test_fake",
            "STRIPE_SECRET_KEY": "sk_test_fake",
            "STRIPE_API_BASE": "http://{name}:12111",
        },
    ),
    # ------------------------------------------------------------------
    # Object storage
    # ------------------------------------------------------------------
    "minio": ServiceRecipe(
        image="minio/minio:latest",
        memory="128m",
        cpus=0.25,
        internal_port=9000,
        environment={
            "MINIO_ROOT_USER": "minioadmin",
            "MINIO_ROOT_PASSWORD": "minioadmin",
        },
        cmd_args='server /data --console-address ":9001"',
        healthcheck="mc ready local || exit 1",
        env_wiring={
            "AWS_ENDPOINT_URL": "http://{name}:9000",
            "AWS_ACCESS_KEY_ID": "minioadmin",
            "AWS_SECRET_ACCESS_KEY": "minioadmin",
            "S3_ENDPOINT": "http://{name}:9000",
        },
    ),
    # ------------------------------------------------------------------
    # Auth providers
    # ------------------------------------------------------------------
    "google-oauth": ServiceRecipe(
        image="ghcr.io/navikt/mock-oauth2-server:2.1.10",
        memory="128m",
        cpus=0.25,
        internal_port=8080,
        environment={"SERVER_PORT": "8080"},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:8080/.well-known/openid-configuration || exit 1",
        env_wiring={
            "GOOGLE_OAUTH_CLIENT_ID": "mock-client-id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "mock-client-secret",
            "GOOGLE_CLIENT_ID": "mock-client-id",
            "GOOGLE_CLIENT_SECRET": "mock-client-secret",
        },
    ),
    "auth0": ServiceRecipe(
        image="ghcr.io/navikt/mock-oauth2-server:2.1.10",
        memory="128m",
        cpus=0.25,
        internal_port=8080,
        environment={"SERVER_PORT": "8080"},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:8080/.well-known/openid-configuration || exit 1",
        env_wiring={
            "AUTH0_DOMAIN": "{name}:8080",
            "AUTH0_CLIENT_ID": "mock-client-id",
            "AUTH0_CLIENT_SECRET": "mock-client-secret",
            "AUTH0_ISSUER_BASE_URL": "http://{name}:8080",
        },
    ),
    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------
    "sendgrid": ServiceRecipe(
        image="ghashange/sendgrid-mock:1.13.0",
        memory="64m",
        cpus=0.25,
        internal_port=3000,
        environment={"API_KEY": "test-key"},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:3000/health || exit 1",
        env_wiring={
            "SENDGRID_API_KEY": "test-key",
            "SENDGRID_API_BASE": "http://{name}:3000",
        },
    ),
    "resend": ServiceRecipe(
        image="axllent/mailpit:latest",
        memory="64m",
        cpus=0.25,
        internal_port=8025,
        environment={},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:8025/api/v1/info || exit 1",
        env_wiring={
            "RESEND_API_KEY": "re_test_fake",
            "SMTP_HOST": "{name}",
            "SMTP_PORT": "1025",
        },
    ),
    "mailgun": ServiceRecipe(
        image="axllent/mailpit:latest",
        memory="64m",
        cpus=0.25,
        internal_port=8025,
        environment={},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:8025/api/v1/info || exit 1",
        env_wiring={
            "MAILGUN_API_KEY": "test-key",
            "SMTP_HOST": "{name}",
            "SMTP_PORT": "1025",
        },
    ),
    # ------------------------------------------------------------------
    # LLM APIs
    # ------------------------------------------------------------------
    "openai": ServiceRecipe(
        image="zerob13/mock-openai-api:latest",
        memory="64m",
        cpus=0.25,
        internal_port=3000,
        environment={},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:3000/v1/models || exit 1",
        env_wiring={
            "OPENAI_API_KEY": "sk-mock-key-for-local-dev",
            "OPENAI_BASE_URL": "http://{name}:3000/v1",
        },
    ),
    # ------------------------------------------------------------------
    # Realtime / WebSockets
    # ------------------------------------------------------------------
    "pusher": ServiceRecipe(
        image="quay.io/soketi/soketi:0.17-16-alpine",
        memory="64m",
        cpus=0.25,
        internal_port=6001,
        environment={
            "DEFAULT_APP_ID": "app-id",
            "DEFAULT_APP_KEY": "app-key",
            "DEFAULT_APP_SECRET": "app-secret",
        },
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:6001/ || exit 1",
        env_wiring={
            "PUSHER_APP_ID": "app-id",
            "PUSHER_KEY": "app-key",
            "PUSHER_SECRET": "app-secret",
            "PUSHER_HOST": "{name}",
            "PUSHER_PORT": "6001",
        },
    ),
    # ------------------------------------------------------------------
    # Search engines
    # ------------------------------------------------------------------
    "elasticsearch": ServiceRecipe(
        image="elasticsearch:8.12.0",
        memory="512m",
        cpus=0.5,
        internal_port=9200,
        environment={
            "discovery.type": "single-node",
            "xpack.security.enabled": "false",
            "ES_JAVA_OPTS": "-Xms256m -Xmx256m",
        },
        cmd_args="",
        healthcheck="curl -sf http://127.0.0.1:9200/_cluster/health || exit 1",
        env_wiring={
            "ELASTICSEARCH_URL": "http://{name}:9200",
            "ELASTIC_URL": "http://{name}:9200",
        },
    ),
    "meilisearch": ServiceRecipe(
        image="getmeili/meilisearch:v1.7",
        memory="128m",
        cpus=0.25,
        internal_port=7700,
        environment={
            "MEILI_ENV": "development",
            "MEILI_NO_ANALYTICS": "true",
        },
        cmd_args="",
        healthcheck="curl -sf http://127.0.0.1:7700/health || exit 1",
        env_wiring={
            "MEILISEARCH_URL": "http://{name}:7700",
            "MEILI_HOST": "http://{name}:7700",
        },
    ),
    "algolia": ServiceRecipe(
        image="getmeili/meilisearch:v1.7",
        memory="128m",
        cpus=0.25,
        internal_port=7700,
        environment={
            "MEILI_ENV": "development",
            "MEILI_NO_ANALYTICS": "true",
        },
        cmd_args="",
        healthcheck="curl -sf http://127.0.0.1:7700/health || exit 1",
        env_wiring={
            "ALGOLIA_APP_ID": "local",
            "ALGOLIA_API_KEY": "local-key",
            "MEILISEARCH_URL": "http://{name}:7700",
        },
    ),
    # ------------------------------------------------------------------
    # Vector databases
    # ------------------------------------------------------------------
    "pinecone": ServiceRecipe(
        image="ghcr.io/pinecone-io/pinecone-local:latest",
        memory="256m",
        cpus=0.25,
        internal_port=5081,
        environment={"PORT": "5081"},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:5081/health || exit 1",
        env_wiring={
            "PINECONE_API_KEY": "local-dev-key",
            "PINECONE_HOST": "http://{name}:5081",
        },
    ),
    "qdrant": ServiceRecipe(
        image="qdrant/qdrant:v1.8",
        memory="128m",
        cpus=0.25,
        internal_port=6333,
        environment={},
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:6333/healthz || exit 1",
        env_wiring={
            "QDRANT_URL": "http://{name}:6333",
        },
    ),
    "weaviate": ServiceRecipe(
        image="semitechnologies/weaviate:1.24.1",
        memory="256m",
        cpus=0.25,
        internal_port=8080,
        environment={
            "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED": "true",
            "PERSISTENCE_DATA_PATH": "/var/lib/weaviate",
            "DEFAULT_VECTORIZER_MODULE": "none",
        },
        cmd_args="",
        healthcheck="wget -qO- http://127.0.0.1:8080/v1/.well-known/ready || exit 1",
        env_wiring={
            "WEAVIATE_URL": "http://{name}:8080",
        },
    ),
    # ------------------------------------------------------------------
    # Message brokers
    # ------------------------------------------------------------------
    "rabbitmq": ServiceRecipe(
        image="rabbitmq:3.13-management-alpine",
        memory="128m",
        cpus=0.25,
        internal_port=5672,
        environment={
            "RABBITMQ_DEFAULT_USER": "guest",
            "RABBITMQ_DEFAULT_PASS": "guest",
        },
        cmd_args="",
        healthcheck="rabbitmq-diagnostics -q check_running",
        env_wiring={
            "RABBITMQ_URL": "amqp://guest:guest@{name}:5672",
            "AMQP_URL": "amqp://guest:guest@{name}:5672",
        },
    ),
    "kafka": ServiceRecipe(
        image="apache/kafka:latest",
        memory="512m",
        cpus=0.5,
        internal_port=9092,
        environment={
            "KAFKA_NODE_ID": "1",
            "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://{name}:9092",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": (
                "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT"
            ),
            "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
            "KAFKA_LOG_RETENTION_HOURS": "1",
            "KAFKA_LOG_SEGMENT_BYTES": "1073741824",
        },
        cmd_args="",
        healthcheck=(
            "/opt/kafka/bin/kafka-broker-api-versions.sh "
            "--bootstrap-server 127.0.0.1:9092 || exit 1"
        ),
        env_wiring={
            "KAFKA_BROKER": "{name}:9092",
            "KAFKA_BOOTSTRAP_SERVERS": "{name}:9092",
        },
    ),
}

# ---------------------------------------------------------------------------
# Service classification sets
# ---------------------------------------------------------------------------

# Services that can safely be disabled (analytics, feature flags, etc.)
DISABLEABLE_SERVICES: set[str] = {
    "posthog",
    "sentry",
    "datadog",
    "launchdarkly",
    "google-maps",
    "mapbox",
}

# Complex services that require agent research to set up
NEEDS_AGENT_RESEARCH: set[str] = {
    "supabase",
    "firebase",
    "clerk",
}

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

# Aliases so callers can use common alternative names
_ALIASES: dict[str, str] = {
    "pg": "postgres",
    "postgresql": "postgres",
    "mongo": "mongodb",
    "aws-s3": "minio",
    "s3": "minio",
    "elastic": "elasticsearch",
    "es": "elasticsearch",
    "meili": "meilisearch",
    "rmq": "rabbitmq",
    "rabbit": "rabbitmq",
    "stripe-mock": "stripe",
    "okta": "auth0",
    "aws-cognito": "auth0",
    "anthropic": "openai",
    "aws-ses": "sendgrid",
    "postmark": "sendgrid",
    "twilio": "sendgrid",
    "svix": "sendgrid",
    "discord": "sendgrid",
}


def get_recipe(service_name: str) -> ServiceRecipe | None:
    """Look up a service recipe by canonical name or common alias.

    Returns ``None`` if no pre-baked recipe exists for *service_name*.
    """
    key = service_name.lower().strip()
    key = _ALIASES.get(key, key)
    return _RECIPES.get(key)


def build_docker_run_cmd(
    recipe: ServiceRecipe,
    container_name: str,
    network_name: str,
    project_name: str,
) -> str:
    """Build a full ``docker run -d`` command string from a :class:`ServiceRecipe`.

    Placeholders in *env_wiring* values (``{name}``) are **not** resolved here
    -- they are intended for the app container's environment, not this command.
    """
    parts: list[str] = [
        "docker run -d",
        f"--name {container_name}",
        f"--network {network_name}",
        f"--label rc-aux-of={project_name}",
        "--restart unless-stopped",
        f"--memory={recipe.memory}",
        f"--cpus={recipe.cpus}",
    ]

    if recipe.shm_size:
        parts.append(f"--shm-size={recipe.shm_size}")

    if recipe.ports:
        for mapping in recipe.ports:
            parts.append(f"-p {mapping}")

    for key, value in recipe.environment.items():
        # Kafka ADVERTISED_LISTENERS contains {name} placeholder that must be
        # resolved to the actual container name at build time.
        resolved = value.replace("{name}", container_name)
        parts.append(f"-e {key}={resolved}")

    # Image
    parts.append(recipe.image)

    # Extra command-line args after the image name
    if recipe.cmd_args:
        parts.append(recipe.cmd_args)

    return " ".join(parts)
