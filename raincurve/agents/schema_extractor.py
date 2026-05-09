from __future__ import annotations

import os
import re
from pathlib import Path

from raincurve.agents.base_agent import CommandResult, _exec_bash


def extract_db_schema(
    project_dir: str,
    db_container: str,
    db_user: str,
    db_name: str,
) -> str:
    """Extract compact DB schema for the seeder agent. Avoids verbose \\d+ output."""
    if not db_container:
        return ""

    sections: list[str] = []

    # Compact column listing — all the agent needs to write INSERT statements
    cols_result = _exec_bash(
        f'docker exec {db_container} psql -U {db_user} -d {db_name} -c "'
        f"SELECT table_name, column_name, data_type, is_nullable, "
        f"COALESCE(column_default, '') as default_val "
        f"FROM information_schema.columns WHERE table_schema = 'public' "
        f"ORDER BY table_name, ordinal_position\"",
        project_dir,
        timeout_s=15,
    )
    if not cols_result.ok:
        return f"(schema extraction failed: {cols_result.stderr})"
    sections.append(f"## Columns\n{cols_result.stdout}")

    # Foreign keys — critical for knowing how tables relate
    fk_result = _exec_bash(
        f'docker exec {db_container} psql -U {db_user} -d {db_name} -c "'
        f"SELECT tc.table_name, kcu.column_name, "
        f"ccu.table_name AS references_table, ccu.column_name AS references_column "
        f"FROM information_schema.table_constraints AS tc "
        f"JOIN information_schema.key_column_usage AS kcu ON tc.constraint_name = kcu.constraint_name "
        f"JOIN information_schema.constraint_column_usage AS ccu ON ccu.constraint_name = tc.constraint_name "
        f"WHERE tc.constraint_type = 'FOREIGN KEY'\"",
        project_dir,
        timeout_s=10,
    )
    if fk_result.ok and fk_result.stdout.strip():
        sections.append(f"## Foreign keys\n{fk_result.stdout}")

    # Enums
    enum_result = _exec_bash(
        f'docker exec {db_container} psql -U {db_user} -d {db_name} -c "'
        f"SELECT t.typname AS enum_name, e.enumlabel AS enum_value "
        f"FROM pg_type t JOIN pg_enum e ON t.oid = e.enumtypid ORDER BY t.typname, e.enumsortorder\"",
        project_dir,
        timeout_s=10,
    )
    if enum_result.ok and enum_result.stdout.strip() and "0 rows" not in enum_result.stdout:
        sections.append(f"## Enums\n{enum_result.stdout}")

    # Existing data sample — show existing user PKs so agent can reference them
    tables_result = _exec_bash(
        f'docker exec {db_container} psql -U {db_user} -d {db_name} -c "\\dt"',
        project_dir,
        timeout_s=10,
    )
    if tables_result.ok:
        table_names = _parse_table_names(tables_result.stdout)
        # Get existing user data (the admin user created by migrations)
        for t in table_names:
            if t in ("user", "users", "account", "accounts"):
                user_sample = _exec_bash(
                    f'docker exec {db_container} psql -U {db_user} -d {db_name} -c '
                    f'"SELECT * FROM \\"{t}\\" LIMIT 5"',
                    project_dir,
                    timeout_s=10,
                )
                if user_sample.ok:
                    sections.append(f"## Existing data in {t}\n{user_sample.stdout}")
                break

    return "\n\n".join(sections)


def extract_api_routes(project_dir: str, app_port: int) -> str:
    """Discover API route paths from codebase. Returns compact file list, not full contents."""
    route_files = _find_route_files(project_dir)
    if not route_files:
        return ""

    # Just list the route file paths — the agent can infer endpoints from file structure
    # (e.g. src/pages/api/auth/login.ts → POST /api/auth/login)
    routes_summary: list[str] = ["Route files found (endpoints inferred from file path):"]
    for rf in route_files:
        rel = str(Path(rf).relative_to(project_dir)).replace("\\", "/")
        routes_summary.append(f"  {rel}")

    # Read only a few key route files for API structure hints
    key_files = [rf for rf in route_files if any(k in rf.lower() for k in ["auth", "user", "login", "signup"])]
    for rf in key_files[:3]:
        try:
            content = Path(rf).read_text(encoding="utf-8", errors="replace")[:1500]
            rel = str(Path(rf).relative_to(project_dir)).replace("\\", "/")
            routes_summary.append(f"\n--- {rel} ---\n{content}")
        except Exception:
            pass

    return "\n".join(routes_summary)


def _parse_table_names(dt_output: str) -> list[str]:
    """Parse table names from psql \\dt output."""
    tables: list[str] = []
    for line in dt_output.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[2] == "table":
            schema = parts[0]
            name = parts[1]
            if schema == "public":
                tables.append(name)
            elif schema and name:
                tables.append(f"{schema}.{name}")
    return tables


def _find_route_files(project_dir: str) -> list[str]:
    """Find files likely to contain API route definitions. Avoids slow recursive globs."""
    skip_dirs = {"node_modules", "__pycache__", ".next", "dist", ".git", ".venv", "venv", "build"}
    target_dirs = {"routes", "api", "controllers", "pages/api", "app/api"}
    target_basenames = {"routes.py", "urls.py", "views.py"}

    found: list[str] = []
    p = Path(project_dir)

    # Walk manually to skip node_modules early (Path.glob traverses everything)
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        root_path = Path(root)
        rel = root_path.relative_to(p)

        # Check if we're inside a target directory
        in_target = any(part in target_dirs for part in rel.parts) or rel.name in target_dirs
        if in_target:
            for f in files:
                if f.endswith((".ts", ".js", ".py")) and not f.endswith(".d.ts"):
                    found.append(str(root_path / f))
        else:
            for f in files:
                if f in target_basenames or f.startswith("router"):
                    found.append(str(root_path / f))

        if len(found) >= 15:
            break

    return sorted(found)[:15]
