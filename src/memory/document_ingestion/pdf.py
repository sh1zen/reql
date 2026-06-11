"""PDF parser backed by pypdf when available."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from ..artifacts.models import SourceArtifact
from .base import DocumentParser
from .metadata import basic_file_metadata, make_fragment
from .models import DocumentParseResult


class PDFParser(DocumentParser):
    parser_name = "pdf"
    parser_version = "pdf-v1"

    def supports(self, artifact: SourceArtifact) -> bool:
        return artifact.artifact_type == "pdf" or (artifact.language or "").casefold() == "pdf"

    def parse(self, artifact: SourceArtifact, content: bytes) -> DocumentParseResult:
        metadata = basic_file_metadata(artifact.path)
        errors: list[str] = []
        fragments = []
        pdf_metadata: dict[str, Any] = {}
        title: str | None = Path(artifact.path).stem

        try:
            pdf_module = import_module("pypdf")
        except Exception:
            metadata.update({"status": "needs_parser", "partially_readable": True, "needs_ocr": True})
            errors.append("PDF parser dependency unavailable; install pypdf for text extraction")
            fragments.append(
                make_fragment(
                    artifact_id=artifact.id,
                    fragment_type="metadata",
                    text=f"PDF artifact metadata only: {Path(artifact.path).name}",
                    index=0,
                    metadata={"parser": self.parser_name, "status": "needs_parser"},
                    confidence=0.6,
                )
            )
            return DocumentParseResult(title=title, metadata=metadata, fragments=fragments, links=[], tables=[], errors=errors, parser_name=self.parser_name, parser_version=self.parser_version)

        PdfReader = pdf_module.PdfReader
        try:
            reader = PdfReader(artifact.path)
            info = getattr(reader, "metadata", None)
            if info:
                pdf_metadata = {str(k).lstrip("/"): str(v) for k, v in dict(info).items() if v is not None}
                title = pdf_metadata.get("Title") or title
            metadata.update({"pdf_metadata": pdf_metadata, "pages": len(reader.pages), "status": "readable"})
            for page_index, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    fragments.append(
                        make_fragment(
                            artifact_id=artifact.id,
                            fragment_type="page",
                            text=page_text.strip(),
                            index=page_index - 1,
                            page_number=page_index,
                            metadata={"parser": self.parser_name},
                        )
                    )
            if not fragments:
                metadata.update({"needs_ocr": True, "partially_readable": True})
                errors.append("PDF text extraction produced no text; OCR may be required")
                fragments.append(
                    make_fragment(
                        artifact_id=artifact.id,
                        fragment_type="metadata",
                        text=f"PDF has {len(reader.pages)} page(s), but no extractable text",
                        index=0,
                        metadata={"parser": self.parser_name, "status": "needs_ocr"},
                        confidence=0.6,
                    )
                )
        except Exception as exc:
            metadata.update({"status": "parser_error", "partially_readable": True})
            errors.append(f"PDF parse failed: {exc}")
            fragments.append(
                make_fragment(
                    artifact_id=artifact.id,
                    fragment_type="metadata",
                    text=f"PDF artifact metadata only: {Path(artifact.path).name}",
                    index=0,
                    metadata={"parser": self.parser_name, "status": "parser_error"},
                    confidence=0.5,
                )
            )
        return DocumentParseResult(title=title, metadata=metadata, fragments=fragments, links=[], tables=[], errors=errors, parser_name=self.parser_name, parser_version=self.parser_version)
