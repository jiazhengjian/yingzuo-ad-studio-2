"""Small, explicit source-release policy. This module never reads .env files."""
from __future__ import annotations

import os
import re
from pathlib import Path

SOURCE_TREES = {"app", "frontend", "tests", "docs", "scripts", ".github"}
ROOT_FILES = {
    ".env.example", ".gitignore", ".gitattributes", ".editorconfig", ".python-version",
    "LICENSE", "NOTICE", "Makefile", "requirements.txt", "requirements-dev.txt",
    "requirements.lock", "requirements-dev.lock", "pyproject.toml", "uv.lock",
    "poetry.lock", "pytest.ini", "package.json", "package-lock.json", "start.command",
}
SOURCE_SUFFIXES = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".svg",
    ".json", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".ini",
    ".cfg", ".sh", ".command", ".lock",
}
EXCLUDED_DIRS = {
    ".git", ".local", "data", "demo", "work", "outputs", ".venv", "venv",
    "node_modules", "dist", "build", ".runtime", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", "coverage", ".vscode", ".idea",
}
PRIVATE_NAMES = {".ds_store", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
PRIVATE_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".log",
    ".pyc", ".pyo", ".tsbuildinfo", ".mp4", ".mov", ".webm", ".mp3",
    ".wav", ".m4a", ".aiff", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".zip", ".tar", ".gz", ".bak", ".swp",
}
MAX_SOURCE_BYTES = 8 * 1024 * 1024
# One README sample may be tracked. Export still omits every audio and video file.
PUBLIC_DEMO_ASSETS = frozenset({Path("docs/demo") / "产品展示.mp4"})


def path_rule(relative: Path) -> str | None:
    """Return a rule name, never sensitive content. Paths must be root-relative."""
    if relative.is_absolute() or ".." in relative.parts:
        return "outside-project"
    if any(part.lower() in EXCLUDED_DIRS for part in relative.parts):
        return "private-or-generated-path"
    if any(part.startswith(".env") for part in relative.parts) and relative != Path(".env.example"):
        return "private-environment-file"
    if relative.name.lower() in PRIVATE_NAMES or relative.suffix.lower() in PRIVATE_SUFFIXES:
        return "private-or-generated-file"
    return None


def is_source(relative: Path) -> bool:
    if path_rule(relative):
        return False
    if len(relative.parts) == 1:
        return relative.name in ROOT_FILES or relative.suffix.lower() in {".md", ".rst", ".py", ".sh", ".command"}
    return relative.parts[0] in SOURCE_TREES and (
        relative.suffix.lower() in SOURCE_SUFFIXES or relative.name in {"LICENSE", "NOTICE", ".gitignore"}
    )


def source_paths(root: Path) -> list[Path]:
    """Allowlist before opening files; prune all runtime trees, never follow links."""
    paths = []
    for current, directories, files in os.walk(root, followlinks=False):
        parent = Path(current).relative_to(root)
        kept = []
        for name in sorted(directories):
            relative = parent / name
            if path_rule(relative) or (parent == Path(".") and name not in SOURCE_TREES):
                continue
            if (root / relative).is_symlink():
                paths.append(relative)
            else:
                kept.append(name)
        directories[:] = kept
        paths.extend(parent / name for name in sorted(files) if is_source(parent / name))
    return sorted(paths)


def placeholder(value: str) -> bool:
    """Only explicit dummy values, never exempt entire test files."""
    low = value.lower()
    return low.startswith(("test_only", "test-only-", "test-secret-placeholder-", "sk-test-", "sk-placeholder-")) or low in {
        "your_api_key", "your-api-key", "replace_me", "changeme", "example", "placeholder",
    }


TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}\b"),
]
ASSIGNMENT = re.compile(
    r"(?i)\b(?:[A-Z0-9_]*API_KEY|[A-Z0-9_]*SECRET|[A-Z0-9_]*ACCESS_TOKEN|[A-Z0-9_]*AUTH_TOKEN|PASSWORD)"
    r"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_./+-]{24,})[\"']"
)
ENV_ASSIGNMENT = re.compile(
    r"(?mi)^(?:[A-Z0-9_]*API_KEY|[A-Z0-9_]*SECRET|[A-Z0-9_]*ACCESS_TOKEN|PASSWORD)"
    r"\s*=\s*([A-Za-z0-9_./+-]{24,})\s*$"
)
BEARER = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_.-]{24,})")
HOME_PATH = re.compile(r"(?:/(?:Users|home)/[^\s/<>\[\]{}'\"\\]+/|[A-Za-z]:\\Users\\[^\s\\<>\[\]{}'\"]+\\|/private/var/" + r"folders/)")
PRIVATE_KEY = re.compile("-----BEGIN " + r"(?:[A-Z0-9]+ )?PRIVATE KEY-----")


def content_rules(data: bytes) -> set[str]:
    if len(data) > MAX_SOURCE_BYTES:
        return {"oversized-source-file"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"non-utf8-source-file"}
    rules = set()
    if "\x00" in text:
        rules.add("binary-source-file")
    if HOME_PATH.search(text):
        rules.add("machine-absolute-path")
    if PRIVATE_KEY.search(text):
        rules.add("private-key-block")
    if any(not placeholder(match.group(0)) for pattern in TOKEN_PATTERNS for match in pattern.finditer(text)):
        rules.add("obvious-token")
    if any(not placeholder(match.group(1)) for pattern in (ASSIGNMENT, ENV_ASSIGNMENT, BEARER) for match in pattern.finditer(text)):
        rules.add("literal-secret")
    return rules


def _inspect_public_demo(root: Path, relative: Path) -> tuple[set[str], bytes | None]:
    """Confirm the committed README sample without treating its bytes as source text."""
    if any((root / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)):
        return {"symlink-not-allowed"}, None
    path = root / relative
    try:
        size = path.stat().st_size
    except OSError:
        return {"source-unreadable"}, None
    if not path.is_file():
        return {"source-unreadable"}, None
    if size <= 0 or size > MAX_SOURCE_BYTES:
        return {"oversized-source-file"}, None
    return set(), None


def inspect_file(root: Path, relative: Path) -> tuple[set[str], bytes | None]:
    if relative in PUBLIC_DEMO_ASSETS:
        return _inspect_public_demo(root, relative)
    rule = path_rule(relative)
    if rule:
        return {rule}, None
    if not is_source(relative):
        return {"not-in-source-allowlist"}, None
    path = root / relative
    if any((root / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)):
        return {"symlink-not-allowed"}, None
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return {"oversized-source-file"}, None
        data = path.read_bytes()
    except OSError:
        return {"source-unreadable"}, None
    return content_rules(data), data
