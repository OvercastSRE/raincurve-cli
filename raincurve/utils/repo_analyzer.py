from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from raincurve.models.repo_brief import (
    ComposeAnalysis,
    ComposeService,
    DockerfileAnalysis,
    EnvVarInfo,
    RepoBrief,
    ServiceRecipe,
)
from raincurve.stubs.detector import KNOWN_SERVICES
from raincurve.utils.repo_scanner import find_key_files, scan_file_tree

try:
    import yaml  # type: ignore[import-untyped]
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENV_FILES = [".env.example", ".env.sample", ".env.template", ".env.development"]

_FRAMEWORK_DEPS_NODE: dict[str, str] = {
    "next": "nextjs",
    "nuxt": "nuxt",
    "express": "express",
    "@nestjs/core": "nestjs",
    "@hono/node-server": "hono",
    "hono": "hono",
    "fastify": "fastify",
    "koa": "koa",
}

_FRAMEWORK_DEPS_PYTHON: dict[str, str] = {
    "django": "django",
    "fastapi": "fastapi",
    "flask": "flask",
    "starlette": "starlette",
}

_FRAMEWORK_DEFAULT_PORTS: dict[str, int] = {
    "nextjs": 3000,
    "nuxt": 3000,
    "express": 3000,
    "nestjs": 3000,
    "hono": 3000,
    "fastify": 3000,
    "koa": 3000,
    "django": 8000,
    "fastapi": 8000,
    "flask": 5000,
    "starlette": 8000,
    "rails": 3000,
    "go": 8080,
}

_EnvPurpose = Literal["database_url", "api_key", "secret", "service_url", "port", "flag", "other"]

_PURPOSE_PATTERNS: list[tuple[str, _EnvPurpose]] = [
    (r"(DATABASE_URL|DB_URL|DB_CONNECTION_STRING)", "database_url"),
    (r"(_URL|_URI)$", "service_url"),
    (r"(_KEY|_SECRET|_TOKEN|_API_KEY)$", "api_key"),
    (r"(^SECRET_KEY$|^JWT_SECRET$|^APP_SECRET$|^SESSION_SECRET$|^ENCRYPTION_KEY$)", "secret"),
    (r"(^PORT$|^HOST$|^BIND$)", "port"),
    (r"(_ENABLED$|_DISABLED$|^ENABLE_|^DISABLE_|^DEBUG$)", "flag"),
]


# ---------------------------------------------------------------------------
# Key-file lookup helpers
# ---------------------------------------------------------------------------


def _path_depth(p: str) -> int:
    return p.count("/") + p.count("\\")


def _find_by_name(key_files: dict[str, str], filename: str) -> tuple[str, str] | None:
    """Find a key file by its basename, preferring shallowest path."""
    matches = [
        (k, v) for k, v in key_files.items()
        if k == filename or k.endswith("/" + filename) or k.endswith("\\" + filename)
    ]
    if not matches:
        return None
    matches.sort(key=lambda kv: _path_depth(kv[0]))
    return matches[0]


def _find_by_names(key_files: dict[str, str], filenames: list[str]) -> tuple[str, str] | None:
    """Try multiple filenames in priority order, return first shallowest match."""
    for name in filenames:
        result = _find_by_name(key_files, name)
        if result:
            return result
    return None


_SKIP_DOCKERFILE_DIRS = {".cursor", ".devcontainer", ".github", ".vscode", ".idea"}


def _find_dockerfile(key_files: dict[str, str]) -> tuple[str, str] | None:
    """Find the best Dockerfile — shallowest, skip IDE dirs, .bak, and test files."""
    matches = [
        (k, v) for k, v in key_files.items()
        if "Dockerfile" in k
        and not k.endswith(".bak")
        and "test" not in k.lower()
        and "cypress" not in k.lower()
        and not any(k.startswith(d + "/") or k.startswith(d + "\\") for d in _SKIP_DOCKERFILE_DIRS)
    ]
    if not matches:
        return None
    matches.sort(key=lambda kv: _path_depth(kv[0]))
    return matches[0]


def _find_compose(key_files: dict[str, str]) -> tuple[str, str] | None:
    """Find the best compose file — shallowest, skip test/cypress directories."""
    compose_names = {
        "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    }
    matches = [
        (k, v) for k, v in key_files.items()
        if any(k.endswith(n) or k.endswith("/" + n) or k.endswith("\\" + n) for n in compose_names)
        and "cypress" not in k.lower()
        and "test" not in k.lower()
    ]
    if not matches:
        return None
    matches.sort(key=lambda kv: _path_depth(kv[0]))
    return matches[0]


# ---------------------------------------------------------------------------
# Sub-analyzers
# ---------------------------------------------------------------------------


def detect_language_and_framework(
    root: Path, key_files: dict[str, str]
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Return (language, version, framework, framework_version, package_manager)."""

    language = "unknown"
    version: str | None = None
    framework: str | None = None
    framework_version: str | None = None
    package_manager: str | None = None

    # -- Node / JavaScript / TypeScript --
    pkg_hit = _find_by_name(key_files, "package.json")
    pyproject_hit = _find_by_name(key_files, "pyproject.toml")
    reqs_hit = _find_by_name(key_files, "requirements.txt")
    pipfile_hit = _find_by_name(key_files, "Pipfile")
    gomod_hit = _find_by_name(key_files, "go.mod")
    gemfile_hit = _find_by_name(key_files, "Gemfile")
    cargo_hit = _find_by_name(key_files, "Cargo.toml")

    # Detect the project subdir (where the main config file lives)
    _project_root = root
    if pkg_hit and ("/" in pkg_hit[0] or "\\" in pkg_hit[0]):
        _project_root = root / Path(pkg_hit[0]).parent
    elif pyproject_hit and ("/" in pyproject_hit[0] or "\\" in pyproject_hit[0]):
        _project_root = root / Path(pyproject_hit[0]).parent
    elif gomod_hit and ("/" in gomod_hit[0] or "\\" in gomod_hit[0]):
        _project_root = root / Path(gomod_hit[0]).parent

    if pkg_hit:
        language = "node"
        pkg: dict[str, Any] = {}
        try:
            pkg = json.loads(pkg_hit[1])
        except (json.JSONDecodeError, ValueError):
            pass

        engines = pkg.get("engines", {})
        version = engines.get("node")

        all_deps: dict[str, str] = {}
        all_deps.update(pkg.get("dependencies", {}))
        all_deps.update(pkg.get("devDependencies", {}))

        for dep_name, fw_name in _FRAMEWORK_DEPS_NODE.items():
            if dep_name in all_deps:
                framework = fw_name
                framework_version = all_deps[dep_name].lstrip("^~>=<")
                break

        if (_project_root / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"
        elif (_project_root / "yarn.lock").exists():
            package_manager = "yarn"
        else:
            package_manager = "npm"

    # -- Python --
    elif pyproject_hit or reqs_hit or pipfile_hit:
        language = "python"
        deps_text = ""
        if reqs_hit:
            deps_text = reqs_hit[1]
        if pyproject_hit:
            deps_text += "\n" + pyproject_hit[1]
        if pipfile_hit:
            deps_text += "\n" + pipfile_hit[1]

        deps_lower = deps_text.lower()
        for dep_name, fw_name in _FRAMEWORK_DEPS_PYTHON.items():
            if dep_name in deps_lower:
                framework = fw_name
                m = re.search(rf"{dep_name}[=~>!<]+([0-9][0-9.]*)", deps_lower)
                if m:
                    framework_version = m.group(1)
                break

        if (_project_root / "Pipfile").exists() or (_project_root / "Pipfile.lock").exists():
            package_manager = "pipenv"
        elif (_project_root / "poetry.lock").exists():
            package_manager = "poetry"
        else:
            package_manager = "pip"

    # -- Go --
    elif gomod_hit:
        language = "go"
        m = re.search(r"^go\s+([\d.]+)", gomod_hit[1], re.MULTILINE)
        if m:
            version = m.group(1)
        package_manager = "go"

    # -- Ruby --
    elif gemfile_hit:
        language = "ruby"
        if "rails" in gemfile_hit[1].lower():
            framework = "rails"
            m = re.search(r"gem\s+['\"]rails['\"],\s*['\"]~?>?\s*([\d.]+)", gemfile_hit[1])
            if m:
                framework_version = m.group(1)
        package_manager = "bundler"

    # -- Rust --
    elif cargo_hit:
        language = "rust"
        package_manager = "cargo"
        cargo_content = cargo_hit[1]
        if "actix" in cargo_content.lower():
            framework = "actix"
        elif "axum" in cargo_content.lower():
            framework = "axum"
        elif "rocket" in cargo_content.lower():
            framework = "rocket"

    # Version from dotfiles (overrides if found)
    for vfile, lang_match in [
        (".node-version", "node"),
        (".nvmrc", "node"),
        (".python-version", "python"),
    ]:
        path = root / vfile
        if path.exists():
            try:
                v = path.read_text(encoding="utf-8", errors="replace").strip()
                if v and language == lang_match:
                    version = v
            except (PermissionError, OSError):
                pass

    # .tool-versions (asdf)
    tv_path = root / ".tool-versions"
    if tv_path.exists():
        try:
            for line in tv_path.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    tool, ver = parts[0], parts[1]
                    if tool in ("nodejs", "node") and language == "node":
                        version = ver
                    elif tool == "python" and language == "python":
                        version = ver
        except (PermissionError, OSError):
            pass

    return language, version, framework, framework_version, package_manager


def analyze_dockerfile(content: str) -> DockerfileAnalysis:
    """Parse a Dockerfile for key directives."""
    base_image = "unknown"
    stages: list[str] = []
    exposed_ports: list[int] = []
    cmd: str | None = None
    has_cache_mounts = False

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # FROM
        m = re.match(r"^FROM\s+(\S+)(?:\s+[Aa][Ss]\s+(\S+))?", stripped, re.IGNORECASE)
        if m:
            base_image = m.group(1)
            if m.group(2):
                stages.append(m.group(2))

        # EXPOSE
        m = re.match(r"^EXPOSE\s+(.+)", stripped, re.IGNORECASE)
        if m:
            for token in m.group(1).split():
                port_str = token.split("/")[0]  # strip /tcp, /udp
                try:
                    exposed_ports.append(int(port_str))
                except ValueError:
                    pass

        # CMD / ENTRYPOINT
        if re.match(r"^(CMD|ENTRYPOINT)\s+", stripped, re.IGNORECASE):
            cmd = stripped

        # Cache mounts
        if "--mount=type=cache" in stripped:
            has_cache_mounts = True

    return DockerfileAnalysis(
        base_image=base_image,
        stages=stages,
        exposed_ports=exposed_ports,
        cmd=cmd,
        has_cache_mounts=has_cache_mounts,
    )


def analyze_compose(content: str) -> ComposeAnalysis | None:
    """Parse a Docker Compose file. Returns None if yaml is unavailable."""
    if yaml is None:
        return None

    try:
        data = yaml.safe_load(content)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    services_data = data.get("services", {})
    if not isinstance(services_data, dict):
        return None

    services: list[ComposeService] = []
    has_build = False

    for name, svc in services_data.items():
        if not isinstance(svc, dict):
            continue

        image = svc.get("image")
        build_ctx = None
        build_val = svc.get("build")
        if isinstance(build_val, str):
            build_ctx = build_val
            has_build = True
        elif isinstance(build_val, dict):
            build_ctx = build_val.get("context", ".")
            has_build = True

        ports_raw = svc.get("ports", [])
        ports = [str(p) for p in ports_raw] if isinstance(ports_raw, list) else []

        env_raw = svc.get("environment", {})
        environment: dict[str, str] = {}
        if isinstance(env_raw, dict):
            environment = {str(k): str(v) for k, v in env_raw.items()}
        elif isinstance(env_raw, list):
            for item in env_raw:
                if "=" in str(item):
                    k, _, v = str(item).partition("=")
                    environment[k] = v

        depends_raw = svc.get("depends_on", [])
        depends_on: list[str] = []
        if isinstance(depends_raw, list):
            depends_on = [str(d) for d in depends_raw]
        elif isinstance(depends_raw, dict):
            depends_on = list(depends_raw.keys())

        volumes_raw = svc.get("volumes", [])
        volumes = [str(v) for v in volumes_raw] if isinstance(volumes_raw, list) else []

        services.append(
            ComposeService(
                name=name,
                image=str(image) if image else None,
                build_context=build_ctx,
                ports=ports,
                environment=environment,
                depends_on=depends_on,
                volumes=volumes,
            )
        )

    uses_env_file = any(
        isinstance(svc, dict) and "env_file" in svc for svc in services_data.values()
    )

    return ComposeAnalysis(
        services=services,
        has_build_directives=has_build,
        uses_env_file=uses_env_file,
    )


def analyze_env_files(root: Path) -> list[EnvVarInfo]:
    """Parse env template files and classify variables."""
    # Build a lookup: env var name -> service name
    env_to_service: dict[str, str] = {}
    for svc in KNOWN_SERVICES:
        for var in svc.env_vars:
            env_to_service[var] = svc.name

    results: list[EnvVarInfo] = []
    seen: set[str] = set()

    # Search root and one level deep for env files
    env_paths: list[tuple[str, Path]] = []
    for filename in _ENV_FILES:
        if (root / filename).exists():
            env_paths.append((filename, root / filename))
        for child in root.iterdir():
            if child.is_dir() and not child.name.startswith(".") and (child / filename).exists():
                env_paths.append((f"{child.name}/{filename}", child / filename))

    for source_name, path in env_paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            continue

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
            if not m:
                continue
            name = m.group(1)
            value = m.group(2).strip().strip("\"'")

            if name in seen:
                continue
            seen.add(name)

            purpose = _classify_env_var(name)
            service = env_to_service.get(name)

            results.append(
                EnvVarInfo(
                    name=name,
                    source_file=source_name,
                    example_value=value if value else None,
                    purpose=purpose,
                    service=service,
                )
            )

    return results


def _classify_env_var(name: str) -> _EnvPurpose:
    """Classify an env var name into a purpose category."""
    for pattern, purpose in _PURPOSE_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return purpose
    return "other"


def detect_database(
    env_vars: list[EnvVarInfo],
    compose: ComposeAnalysis | None,
    deps_text: str,
) -> tuple[str | None, str | None]:
    """Return (db_type, url_pattern) by checking compose, env vars, and deps."""

    # 1. From compose service images
    if compose:
        for svc in compose.services:
            img = (svc.image or "").lower()
            if "postgres" in img:
                return "postgresql", "postgresql://postgres:postgres@db:5432/app"
            if "mysql" in img or "mariadb" in img:
                return "mysql", "mysql://root:root@db:3306/app"
            if "mongo" in img:
                return "mongodb", "mongodb://db:27017/app"

    # 2. From DATABASE_URL value pattern
    for ev in env_vars:
        if ev.name in ("DATABASE_URL", "DB_URL") and ev.example_value:
            val = ev.example_value.lower()
            if "postgresql" in val or "postgres" in val:
                return "postgresql", ev.example_value
            if "mysql" in val:
                return "mysql", ev.example_value
            if "mongodb" in val:
                return "mongodb", ev.example_value
            if "sqlite" in val:
                return "sqlite", ev.example_value

    # 3. From dependency names
    deps_lower = deps_text.lower()
    dep_db_map = [
        (["prisma", "pg", "psycopg2", "psycopg", "asyncpg", "pgx", "sqlx"], "postgresql"),
        (["mysql2", "mysqlclient", "pymysql"], "mysql"),
        (["mongoose", "pymongo", "mongodb", "mongoclient"], "mongodb"),
    ]
    for dep_names, db_type in dep_db_map:
        for dep in dep_names:
            if dep in deps_lower:
                return db_type, None

    return None, None


def detect_migration_tool(
    root: Path,
    language: str,
    framework: str | None,
    deps_text: str,
    key_files: dict[str, str],
) -> tuple[str | None, str | None, str | None]:
    """Return (tool, migrate_command, seed_command)."""

    deps_lower = deps_text.lower()

    # Check package.json scripts for custom migrate/seed commands
    pkg_scripts: dict[str, str] = {}
    if "package.json" in key_files:
        try:
            pkg = json.loads(key_files["package.json"])
            pkg_scripts = pkg.get("scripts", {})
        except (json.JSONDecodeError, ValueError):
            pass

    # Prisma
    if "prisma" in deps_lower or (root / "prisma" / "schema.prisma").exists():
        seed_cmd = "npx prisma db seed" if "seed" in pkg_scripts else None
        return "prisma", "npx prisma migrate deploy", seed_cmd

    # Django
    if framework == "django" or (root / "manage.py").exists():
        return "django", "python manage.py migrate", "python manage.py loaddata"

    # Alembic
    if "alembic" in deps_lower or (root / "alembic.ini").exists():
        return "alembic", "alembic upgrade head", None

    # Knex
    if "knex" in deps_lower:
        seed_cmd = "npx knex seed:run" if "seed" in deps_lower else None
        return "knex", "npx knex migrate:latest", seed_cmd

    # Sequelize
    if "sequelize" in deps_lower:
        return "sequelize", "npx sequelize-cli db:migrate", "npx sequelize-cli db:seed:all"

    # TypeORM
    if "typeorm" in deps_lower:
        return "typeorm", "npx typeorm migration:run", None

    # Drizzle
    if "drizzle-kit" in deps_lower:
        return "drizzle", "npx drizzle-kit push", None

    # Check package.json scripts as fallback
    for script_name, script_cmd in pkg_scripts.items():
        if "migrate" in script_name.lower():
            pm = "npm"
            if (root / "pnpm-lock.yaml").exists():
                pm = "pnpm"
            elif (root / "yarn.lock").exists():
                pm = "yarn"
            migrate_cmd = f"{pm} run {script_name}"
            seed_cmd = None
            for s_name in pkg_scripts:
                if "seed" in s_name.lower():
                    seed_cmd = f"{pm} run {s_name}"
                    break
            return script_name, migrate_cmd, seed_cmd

    # Rails
    if framework == "rails":
        return "rails", "bundle exec rails db:migrate", "bundle exec rails db:seed"

    return None, None, None


def detect_app_port(
    dockerfile: DockerfileAnalysis | None,
    compose: ComposeAnalysis | None,
    framework: str | None,
    env_vars: list[EnvVarInfo],
) -> int | None:
    """Determine the application port. Priority: Dockerfile > compose > env > framework default."""

    # 1. Dockerfile EXPOSE
    if dockerfile and dockerfile.exposed_ports:
        return dockerfile.exposed_ports[0]

    # 2. Compose ports for services with build directives
    if compose:
        for svc in compose.services:
            if svc.build_context and svc.ports:
                for p in svc.ports:
                    # Parse "3000:3000" or "8080:3000" -> container port (right side)
                    parts = str(p).split(":")
                    try:
                        return int(parts[-1].split("/")[0])
                    except ValueError:
                        continue

    # 3. PORT env var
    for ev in env_vars:
        if ev.name == "PORT" and ev.example_value:
            try:
                return int(ev.example_value)
            except ValueError:
                pass

    # 4. Framework default
    if framework and framework in _FRAMEWORK_DEFAULT_PORTS:
        return _FRAMEWORK_DEFAULT_PORTS[framework]

    return None


def _detect_start_command(
    key_files: dict[str, str],
    framework: str | None,
    language: str,
) -> str | None:
    """Best-effort detection of the start command."""
    # package.json scripts
    if "package.json" in key_files:
        try:
            pkg = json.loads(key_files["package.json"])
            scripts = pkg.get("scripts", {})
            for candidate in ("start", "serve", "dev"):
                if candidate in scripts:
                    return f"npm run {candidate}"
        except (json.JSONDecodeError, ValueError):
            pass

    # Procfile
    if "Procfile" in key_files:
        for line in key_files["Procfile"].splitlines():
            if line.startswith("web:"):
                return line.split(":", 1)[1].strip()

    # Framework defaults
    if framework == "django":
        return "python manage.py runserver 0.0.0.0:8000"
    if framework == "fastapi":
        return "uvicorn main:app --host 0.0.0.0 --port 8000"
    if framework == "flask":
        return "flask run --host 0.0.0.0"

    return None


def _detect_build_command(key_files: dict[str, str]) -> str | None:
    """Best-effort detection of the build command."""
    if "package.json" in key_files:
        try:
            pkg = json.loads(key_files["package.json"])
            scripts = pkg.get("scripts", {})
            if "build" in scripts:
                return "npm run build"
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _gather_deps_text(key_files: dict[str, str]) -> str:
    """Combine all dependency-related file contents into one string for searching."""
    parts: list[str] = []
    for fname in (
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "Pipfile",
        "go.mod",
        "Gemfile",
        "Cargo.toml",
    ):
        hit = _find_by_name(key_files, fname)
        if hit:
            parts.append(hit[1])
    return "\n".join(parts)


def _build_service_recipes(
    env_vars: list[EnvVarInfo],
    compose: ComposeAnalysis | None,
    db_type: str | None,
) -> list[ServiceRecipe]:
    """Build ServiceRecipe entries for detected auxiliary services."""
    recipes: list[ServiceRecipe] = []
    seen: set[str] = set()

    # Database from compose
    if compose:
        for svc in compose.services:
            img = (svc.image or "").lower()
            if "postgres" in img and "postgres" not in seen:
                seen.add("postgres")
                recipes.append(
                    ServiceRecipe(
                        name="postgres",
                        image=svc.image or "postgres:16-alpine",
                        environment=svc.environment or {"POSTGRES_PASSWORD": "postgres"},
                        healthcheck="pg_isready -U postgres",
                    )
                )
            elif ("mysql" in img or "mariadb" in img) and "mysql" not in seen:
                seen.add("mysql")
                recipes.append(
                    ServiceRecipe(
                        name="mysql",
                        image=svc.image or "mysql:8",
                        environment=svc.environment
                        or {"MYSQL_ROOT_PASSWORD": "root", "MYSQL_DATABASE": "app"},
                        healthcheck="mysqladmin ping -h localhost",
                    )
                )
            elif "mongo" in img and "mongo" not in seen:
                seen.add("mongo")
                recipes.append(
                    ServiceRecipe(
                        name="mongo",
                        image=svc.image or "mongo:7",
                        healthcheck="mongosh --eval 'db.runCommand(\"ping\")'",
                    )
                )
            elif "redis" in img and "redis" not in seen:
                seen.add("redis")
                recipes.append(
                    ServiceRecipe(
                        name="redis",
                        image=svc.image or "redis:7-alpine",
                        healthcheck="redis-cli ping",
                    )
                )

    # Database from detection (if not already found via compose)
    if db_type and db_type not in seen:
        db_recipes: dict[str, ServiceRecipe] = {
            "postgresql": ServiceRecipe(
                name="postgres",
                image="postgres:16-alpine",
                environment={"POSTGRES_PASSWORD": "postgres", "POSTGRES_DB": "app"},
                healthcheck="pg_isready -U postgres",
            ),
            "mysql": ServiceRecipe(
                name="mysql",
                image="mysql:8",
                environment={"MYSQL_ROOT_PASSWORD": "root", "MYSQL_DATABASE": "app"},
                healthcheck="mysqladmin ping -h localhost",
            ),
            "mongodb": ServiceRecipe(
                name="mongo",
                image="mongo:7",
                healthcheck="mongosh --eval 'db.runCommand(\"ping\")'",
            ),
        }
        if db_type in db_recipes:
            recipes.append(db_recipes[db_type])
            seen.add(db_type)

    # Redis from env vars (if not already in compose)
    if "redis" not in seen:
        for ev in env_vars:
            if ev.service == "redis" or ev.name in ("REDIS_URL", "REDIS_HOST"):
                recipes.append(
                    ServiceRecipe(
                        name="redis",
                        image="redis:7-alpine",
                        healthcheck="redis-cli ping",
                    )
                )
                seen.add("redis")
                break

    return recipes


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def analyze_repo(root: str | Path) -> RepoBrief:
    """Perform deep deterministic repo analysis with zero LLM calls.

    Scans the file tree, parses key config files, detects language/framework,
    databases, migrations, services, and returns a complete RepoBrief.
    """
    root = Path(root)

    # 1. Use existing repo_scanner utilities
    file_tree = scan_file_tree(root)
    key_files = find_key_files(root)

    # 2. Language & framework
    language, lang_version, framework, fw_version, package_manager = detect_language_and_framework(
        root, key_files
    )

    # 3. Dockerfile analysis
    dockerfile_analysis: DockerfileAnalysis | None = None
    dockerfile_path: str | None = None
    has_dockerfile = False

    df_hit = _find_dockerfile(key_files)
    if df_hit:
        dockerfile_path = df_hit[0]
        dockerfile_analysis = analyze_dockerfile(df_hit[1])
        has_dockerfile = True

    # 4. Compose analysis
    compose_analysis: ComposeAnalysis | None = None
    compose_path: str | None = None
    has_compose = False

    compose_hit = _find_compose(key_files)
    if compose_hit:
        compose_path = compose_hit[0]
        compose_analysis = analyze_compose(compose_hit[1])
        has_compose = compose_analysis is not None

    # 5. .dockerignore
    has_dockerignore = (root / ".dockerignore").exists()

    # 6. Env vars
    env_vars = analyze_env_files(root)

    # 7. Dependencies text for searching
    deps_text = _gather_deps_text(key_files)

    # 8. Database
    db_type, db_url_pattern = detect_database(env_vars, compose_analysis, deps_text)

    # 9. Migration tool
    migration_tool, migration_command, seed_command = detect_migration_tool(
        root, language, framework, deps_text, key_files
    )

    # 10. App port
    app_port = detect_app_port(dockerfile_analysis, compose_analysis, framework, env_vars)

    # 11. Start & build commands
    start_command = _detect_start_command(key_files, framework, language)
    build_command = _detect_build_command(key_files)

    # 12. Service recipes
    detected_services = _build_service_recipes(env_vars, compose_analysis, db_type)

    return RepoBrief(
        language=language,
        language_version=lang_version,
        framework=framework,
        framework_version=fw_version,
        package_manager=package_manager,
        has_dockerfile=has_dockerfile,
        dockerfile_path=dockerfile_path,
        dockerfile_analysis=dockerfile_analysis,
        has_compose=has_compose,
        compose_path=compose_path,
        compose_analysis=compose_analysis,
        has_dockerignore=has_dockerignore,
        app_port=app_port,
        start_command=start_command,
        build_command=build_command,
        env_vars=env_vars,
        database_type=db_type,
        database_url_pattern=db_url_pattern,
        migration_tool=migration_tool,
        migration_command=migration_command,
        seed_command=seed_command,
        detected_services=detected_services,
        file_tree=file_tree,
        key_file_contents=key_files,
    )
