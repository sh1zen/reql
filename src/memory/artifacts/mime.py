"""Lightweight artifact type and language detection."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from ..code_analysis.catalog import display_language_for_path
from .models import ArtifactType

TEXT_EXTENSIONS = {
    ".css",
    ".htm",
    ".html",
    ".rts",
    ".txt",
    ".rst",
    ".log",
}

DATA_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".xml",
    ".ndjson",
}

UNSUPPORTED_MEDIA_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".png",
    ".tif",
    ".tiff",
    ".webm",
    ".webp",
}

CONFIG_EXTENSIONS = {
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".conf",
}

CONFIG_LANGUAGES = {
    ".json": "JSON",
    ".toml": "TOML",
    ".yaml": "YAML",
    ".yml": "YAML",
}

TEXT_LANGUAGES = {
    ".css": "CSS",
    ".htm": "HTML",
    ".html": "HTML",
    ".rst": "reStructuredText",
    ".rts": "Text",
    ".sql": "SQL",
}

DOCUMENT_STEMS = {"readme", "changelog", "changes", "faq", "frequently-asked-questions"}
WORDPRESS_TITLE_RE = re.compile(r"^===\s*\S.*?\s*===$")
WORDPRESS_SECTION_RE = re.compile(r"^==\s*\S.*?\s*==$")
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
MARKDOWN_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
MARKDOWN_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
MARKDOWN_SETEXT_RE = re.compile(r"^\s*(?:=+|-+)\s*$")


@dataclass(frozen=True, slots=True)
class Classification:
    artifact_type: ArtifactType
    language: str | None
    binary: bool


def is_binary_sample(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    text_bytes = {7, 8, 9, 10, 12, 13, 27}
    non_text = sum(1 for byte in sample if byte < 32 and byte not in text_bytes)
    return non_text / max(len(sample), 1) > 0.30


def classify_path(path: str | Path, sample: bytes) -> Classification:
    suffix = Path(path).suffix.casefold()
    lower_name = Path(path).name.casefold()
    stem = Path(path).stem.casefold()

    if sample.startswith(b"%PDF-"):
        return Classification("pdf", "PDF", True)

    if suffix == ".pdf":
        return Classification("pdf", "PDF", True)

    binary = is_binary_sample(sample)
    if binary:
        return Classification("binary", None, True)

    code_language = display_language_for_path(path)
    if code_language:
        return Classification("code", code_language, False)
    if suffix in {".md", ".markdown"}:
        return Classification("markdown", "Markdown", False)
    if suffix in {"", ".txt"} and stem in DOCUMENT_STEMS:
        return Classification("markdown", "Markdown", False)
    if _looks_like_wordpress_readme(sample):
        return Classification("markdown", "Markdown", False)
    if not suffix and _looks_like_structured_markdown(sample):
        return Classification("markdown", "Markdown", False)
    if suffix in CONFIG_EXTENSIONS:
        return Classification("config", CONFIG_LANGUAGES.get(suffix), False)
    if suffix in DATA_EXTENSIONS:
        return Classification("data", None, False)
    if suffix in TEXT_EXTENSIONS or lower_name in {"license", "notice"}:
        return Classification("text", TEXT_LANGUAGES.get(suffix), False)

    head = sample[:512].decode("utf-8", errors="ignore").lstrip()
    if head.startswith("#!"):
        shebang = head.splitlines()[0].casefold()
        if "python" in shebang:
            return Classification("code", "Python", False)
        if "node" in shebang:
            return Classification("code", "JavaScript", False)
        if "bash" in shebang or shebang.endswith("/sh") or " sh" in shebang:
            return Classification("code", "Bash", False)
        if "pwsh" in shebang or "powershell" in shebang:
            return Classification("code", "PowerShell", False)
        if "ruby" in shebang:
            return Classification("code", "Ruby", False)
        if "php" in shebang:
            return Classification("code", "PHP", False)
    if head.startswith("{") or head.startswith("["):
        return Classification("data", "JSON", False)
    if head.startswith("# "):
        return Classification("markdown", "Markdown", False)
    return Classification("unknown", None, False)


def _sample_lines(sample: bytes) -> list[str]:
    return sample.decode("utf-8", errors="ignore").splitlines()[:120]


def _looks_like_wordpress_readme(sample: bytes) -> bool:
    lines = [line.strip() for line in _sample_lines(sample) if line.strip()]
    has_title = any(WORDPRESS_TITLE_RE.match(line) for line in lines)
    has_section = any(WORDPRESS_SECTION_RE.match(line) for line in lines)
    header_names = {"contributors", "tags", "requires at least", "tested up to", "stable tag", "license"}
    header_count = sum(1 for line in lines if line.partition(":")[0].strip().casefold() in header_names and ":" in line)
    return has_title and (has_section or header_count >= 2)


def _looks_like_structured_markdown(sample: bytes) -> bool:
    lines = _sample_lines(sample)
    signals = 0
    if any(MARKDOWN_HEADING_RE.match(line) for line in lines):
        signals += 2
    if any(MARKDOWN_FENCE_RE.match(line) for line in lines):
        signals += 2
    if any(
        index > 0 and lines[index - 1].strip() and MARKDOWN_SETEXT_RE.match(line)
        for index, line in enumerate(lines)
    ):
        signals += 2
    if sum(1 for line in lines if MARKDOWN_LIST_RE.match(line)) >= 2:
        signals += 1
    return signals >= 2


def is_unsupported_media_file(path: str | Path, sample: bytes) -> bool:
    suffix = Path(path).suffix.casefold()
    if suffix in UNSUPPORTED_MEDIA_EXTENSIONS:
        return True
    return (
        sample.startswith(b"\x89PNG\r\n\x1a\n")
        or sample.startswith(b"\xff\xd8\xff")
        or sample.startswith(b"GIF87a")
        or sample.startswith(b"GIF89a")
        or sample.startswith(b"RIFF") and sample[8:12] == b"WEBP"
        or len(sample) >= 12 and sample[4:8] == b"ftyp"
    )
