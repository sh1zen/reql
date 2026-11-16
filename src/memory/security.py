"""Security helpers for untrusted IO and agent-facing text boundaries."""
from __future__ import annotations

import html
import os
from pathlib import Path

MCP_ALLOWED_ROOTS_ENV = "REQL_MCP_ALLOWED_ROOTS"


class SecurityError(ValueError):
    """Raised when untrusted input violates a security boundary."""


def sanitize_label(value: object, *, max_chars: int = 256) -> str:
    """Return a bounded, escaped string suitable for agent or HTML-adjacent output."""

    text = str(value or "")
    text = "".join(" " if _is_control(char) else char for char in text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return html.escape(text, quote=True)


def sanitize_agent_text(value: object, *, max_chars: int = 2000) -> str:
    """Sanitize user-controlled text before returning it to an assistant client."""

    text = str(value or "")
    text = "".join("\n" if char in {"\n", "\r"} else " " if _is_control(char) else char for char in text)
    text = _neutralize_agent_sentinels(text)
    text = html.escape(text, quote=True)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def validate_mcp_path(path: str | Path, *, name: str, must_exist: bool = False) -> Path:
    """Resolve an MCP-supplied local path and require it to stay under allowed roots."""

    raw = str(path)
    if not raw.strip():
        raise SecurityError(f"{name} must be a non-empty path")
    if "\x00" in raw:
        raise SecurityError(f"{name} cannot contain NUL bytes")
    if "://" in raw:
        raise SecurityError(f"{name} must be a local filesystem path")
    resolved = Path(raw).expanduser().resolve(strict=False)
    if must_exist and not resolved.exists():
        raise SecurityError(f"{name} does not exist: {resolved}")
    candidate = resolved if resolved.exists() or not resolved.suffix else resolved.parent
    roots = _mcp_allowed_roots()
    if not any(_is_relative_to(candidate, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise SecurityError(f"{name} must be inside an allowed MCP root: {allowed}")
    return resolved


def _mcp_allowed_roots() -> list[Path]:
    raw = os.environ.get(MCP_ALLOWED_ROOTS_ENV, "")
    roots = [part for part in raw.split(os.pathsep) if part.strip()]
    if not roots:
        roots = [str(Path.cwd())]
    return [Path(root).expanduser().resolve(strict=False) for root in roots]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_control(char: str) -> bool:
    return ord(char) < 32 or ord(char) == 127


def _neutralize_agent_sentinels(text: str) -> str:
    replacements = {
        "<|": "<\u200b|",
        "|>": "|\u200b>",
        "</": "<\u200b/",
        "[INST]": "[\u200bINST]",
        "[/INST]": "[\u200b/INST]",
        "<<SYS>>": "<\u200b<SYS>>",
        "<</SYS>>": "<\u200b</SYS>>",
    }
    for needle, replacement in replacements.items():
        text = text.replace(needle, replacement)
    return text
