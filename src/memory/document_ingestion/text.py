"""Plain text parser."""
from __future__ import annotations

from pathlib import Path

from ..artifacts.models import SourceArtifact
from .base import DocumentParser
from .chunking import paragraph_chunks
from .metadata import basic_file_metadata, make_fragment
from .models import DocumentParseResult


class PlainTextParser(DocumentParser):
    parser_name = "plain_text"
    parser_version = "text-v1"

    def __init__(self, *, max_chars: int = 3000) -> None:
        self.max_chars = max_chars

    def supports(self, artifact: SourceArtifact) -> bool:
        return artifact.artifact_type in {"text", "code", "config", "data", "unknown"}

    def parse(self, artifact: SourceArtifact, content: bytes) -> DocumentParseResult:
        text = content.decode("utf-8", errors="replace")
        metadata = basic_file_metadata(artifact.path)
        metadata.update({"language": artifact.language, "artifact_type": artifact.artifact_type})
        fragments = [
            make_fragment(
                artifact_id=artifact.id,
                fragment_type="raw_text" if artifact.artifact_type == "code" else "paragraph",
                text=chunk.text,
                index=index,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                metadata={"parser": self.parser_name},
            )
            for index, chunk in enumerate(paragraph_chunks(text, max_chars=self.max_chars))
        ]
        if not fragments:
            fragments.append(
                make_fragment(
                    artifact_id=artifact.id,
                    fragment_type="metadata",
                    text=f"Empty text artifact: {Path(artifact.path).name}",
                    index=0,
                    metadata={"parser": self.parser_name},
                )
            )
        return DocumentParseResult(
            title=Path(artifact.path).stem,
            metadata=metadata,
            fragments=fragments,
            links=[],
            tables=[],
            errors=[],
            parser_name=self.parser_name,
            parser_version=self.parser_version,
        )
