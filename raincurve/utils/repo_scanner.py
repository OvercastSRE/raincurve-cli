from __future__ import annotations

from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "dist", "build", ".next", ".nuxt", ".output",
    ".raincurve", ".docker", "vendor",
}

KEY_FILES = [
    "README.md",
    "readme.md",
    "Readme.md",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    ".env.example",
    ".env.sample",
    ".env.template",
    "package.json",
    "requirements.txt",
    "Pipfile",
    "pyproject.toml",
    "go.mod",
    "Gemfile",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Makefile",
    "Procfile",
]

MAX_TREE_DEPTH = 4
MAX_FILE_READ_BYTES = 8192


def scan_file_tree(root: str | Path, max_depth: int = MAX_TREE_DEPTH) -> str:
    root = Path(root)
    lines: list[str] = []

    def _walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except (PermissionError, OSError):
            return

        dirs = []
        files = []
        for e in entries:
            try:
                if e.is_dir() and e.name not in SKIP_DIRS and not e.name.startswith("."):
                    dirs.append(e)
                elif e.is_file():
                    files.append(e)
            except OSError:
                continue

        for f in files:
            lines.append(f"{prefix}{f.name}")
        for d in dirs:
            lines.append(f"{prefix}{d.name}/")
            _walk(d, prefix + "  ", depth + 1)

    _walk(root, "", 0)
    return "\n".join(lines)


def find_key_files(root: str | Path) -> dict[str, str]:
    root = Path(root)
    found: dict[str, str] = {}

    search_dirs = [root]
    try:
        for child in root.iterdir():
            try:
                if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
                    search_dirs.append(child)
            except OSError:
                continue
    except OSError:
        pass

    for search_dir in search_dirs:
        for name in KEY_FILES:
            path = search_dir / name
            if path.exists():
                rel = str(path.relative_to(root))
                if rel not in found:
                    try:
                        content = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_READ_BYTES]
                        found[rel] = content
                    except (PermissionError, OSError):
                        pass

    _skip_prefixes = {".cursor", ".devcontainer", ".github", ".vscode", ".idea"}
    try:
        for dockerfile in root.rglob("Dockerfile*"):
            try:
                if not dockerfile.is_file():
                    continue
            except OSError:
                continue
            rel = str(dockerfile.relative_to(root))
            first_part = Path(rel).parts[0] if Path(rel).parts else ""
            if rel not in found and not any(skip in rel for skip in SKIP_DIRS) and first_part not in _skip_prefixes:
                try:
                    found[rel] = dockerfile.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_READ_BYTES]
                except (PermissionError, OSError):
                    pass
    except OSError:
        pass

    for pattern in ["docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml"]:
        try:
            for compose in root.rglob(pattern):
                try:
                    if not compose.is_file():
                        continue
                except OSError:
                    continue
                rel = str(compose.relative_to(root))
                if rel not in found and not any(skip in rel for skip in SKIP_DIRS):
                    try:
                        found[rel] = compose.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_READ_BYTES]
                    except (PermissionError, OSError):
                        pass
        except OSError:
            pass

    return found


def build_repo_context(root: str | Path) -> str:
    root = Path(root)
    tree = scan_file_tree(root)
    key_files = find_key_files(root)

    parts = [f"# File Tree\n\n```\n{tree}\n```\n"]
    for name, content in key_files.items():
        parts.append(f"# {name}\n\n```\n{content}\n```\n")

    return "\n".join(parts)
