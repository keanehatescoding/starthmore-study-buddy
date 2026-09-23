"""Text extraction per resource type. No LLM here.

Contract with sync (important): content_hash stays the RAW-content hash
computed at sync time. Extraction only fills extracted_text + status, so the
next sync's hash check keeps working instead of re-queueing forever.

Outcomes per resource:
  extracted -> extracted_text set, status "extracted"
  unusable video / empty doc -> status "skipped" (never silently stuck)
  broken file -> status "failed" + error
"""

from __future__ import annotations

import io
from typing import Callable


class ExtractError(RuntimeError):
    pass


class SkipResource(RuntimeError):
    pass


def extract_pdf(blob: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(blob))
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = "\n\n".join(t.strip() for t in pages if t.strip())
    if not text.strip():
        raise ExtractError("PDF has no extractable text (scanned images?)")
    return text


def extract_pptx(blob: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(blob))
    slides = []
    for i, slide in enumerate(prs.slides, 1):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text.strip()
                if t:
                    parts.append(t)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
        if parts:
            slides.append(f"--- Slide {i} ---\n" + "\n".join(parts))
    text = "\n\n".join(slides)
    if not text.strip():
        raise ExtractError("PPTX has no extractable text")
    return text


def extract_docx(blob: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(blob))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n\n".join(parts)
    if not text.strip():
        raise ExtractError("DOCX has no extractable text")
    return text


def extract_transcript(url: str) -> str:
    """YouTube transcript. Raises SkipResource when unavailable (v1 policy)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    try:
        from urllib.parse import parse_qs, urlparse

        if "youtu.be" in url:
            video_id = urlparse(url).path.strip("/").split("/")[0]
        else:
            video_id = parse_qs(urlparse(url).query).get("v", [None])[0]
        if not video_id:
            raise SkipResource(f"cannot parse video id from {url}")
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id)
        text = " ".join(s.text.strip() for s in transcript if s.text.strip())
        if not text.strip():
            raise SkipResource(f"empty transcript for {url}")
        return text
    except SkipResource:
        raise
    except Exception as e:
        raise SkipResource(f"no transcript for {url}: {e}") from e


def extract_bytes(blob: bytes, mime: str | None, filename: str = "") -> str:
    name = filename.lower()
    mime = (mime or "").lower()
    if "pdf" in mime or name.endswith(".pdf"):
        return extract_pdf(blob)
    if "presentation" in mime or name.endswith(".pptx"):
        return extract_pptx(blob)
    if "wordprocessing" in mime or name.endswith(".docx"):
        return extract_docx(blob)
    if mime.startswith("text/") or name.endswith((".txt", ".md", ".csv", ".html")):
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            return blob.decode("latin-1")
    raise ExtractError(f"unsupported type (mime={mime or '?'}, file={filename or '?'})")


Downloader = Callable[[str], "tuple[bytes, str | None]"]


def extract_resource_text(resource, downloader: Downloader | None = None) -> str:
    """Return extracted text for a Resource row. Raises SkipResource/ExtractError."""
    if resource.type == "page_text":
        if resource.extracted_text:
            return resource.extracted_text
        raise SkipResource("page had no retrievable content at sync time")
    if resource.type == "video":
        if not resource.raw_url:
            raise SkipResource("video has no URL")
        return extract_transcript(resource.raw_url)
    if resource.type == "link":
        raise SkipResource("generic links are not extracted in v1")
    if resource.type == "file":
        if downloader is None:
            raise ExtractError("no downloader available for file resource")
        filename = (resource.raw_url or "").split("?")[0].rsplit("/", 1)[-1]
        blob, mime = downloader(resource.raw_url)
        return extract_bytes(blob, mime or resource.mime_type, filename)
    raise ExtractError(f"unknown resource type {resource.type!r}")
