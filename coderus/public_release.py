from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

IGNORED_DIRECTORIES = frozenset(
    {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
)
FORBIDDEN_DIRECTORIES = frozenset({"data", "dist", "output"})
FORBIDDEN_NAMES = frozenset(
    {
        ".env",
        "config.yaml",
        "secrets.env",
        "id_rsa",
        "id_ed25519",
    }
)
FORBIDDEN_SUFFIXES = (".db", ".db-shm", ".db-wal", ".pem", ".p12", ".pfx")

CONTENT_RULES = (
    (
        "private key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "GitHub token",
        re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    ),
    (
        "API key",
        re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    ),
    (
        "Windows user path",
        re.compile(rb"(?i)[a-z]:\\" rb"Users\\(?!Public\\|Default\\|example\\|<user>\\)"),
    ),
    (
        "Unix user path",
        re.compile(rb"/" rb"home/(?!coderus(?:/|\b)|example(?:/|\b)|<user>(?:/|\b))"),
    ),
)


def scan_public_files(root: Path, paths: Iterable[Path]) -> list[str]:
    root = root.resolve()
    findings: list[str] = []
    for path in paths:
        resolved = path.resolve()
        relative_path = resolved.relative_to(root)
        relative = relative_path.as_posix()
        lower_parts = tuple(part.lower() for part in relative_path.parts)
        lower_name = resolved.name.lower()
        if FORBIDDEN_DIRECTORIES.intersection(lower_parts):
            findings.append(f"{relative}: runtime directory")
        if (
            lower_name in FORBIDDEN_NAMES
            or lower_name.startswith(".env.")
            or relative.lower().endswith(FORBIDDEN_SUFFIXES)
        ):
            findings.append(f"{relative}: forbidden file")
        try:
            content = resolved.read_bytes()
        except OSError as exc:
            findings.append(f"{relative}: cannot read ({exc})")
            continue
        if b"\0" in content:
            continue
        for label, pattern in CONTENT_RULES:
            if pattern.search(content):
                findings.append(f"{relative}: {label}")
    return sorted(set(findings))


def require_public_files(root: Path, paths: Iterable[Path]) -> None:
    findings = scan_public_files(root, paths)
    if findings:
        details = "\n".join(f"- {finding}" for finding in findings)
        raise ValueError(f"public release rejected:\n{details}")
