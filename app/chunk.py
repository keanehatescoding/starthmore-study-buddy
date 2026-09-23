"""LLM semantic chunking. One cheap-model call per section.

- Long docs (>MAX_SECTION_CHARS) are pre-split on paragraph boundaries
  before chunking, since chunk quality degrades on very long inputs.
- Short texts skip the LLM entirely (single chunk, no cost).
- Cache: resources that already have chunks and status "extracted" are
  skipped; status "pending" with stale chunks means content changed ->
  delete + regenerate (no versioning, per plan).
- Offsets are located locally with str.find (model ranges are unreliable),
  so Chunk.start_char/end_char support a future "show source" UI.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.llm import LLMClient
from app.models import Chunk

MAX_SECTION_CHARS = 10000
MIN_LLM_CHARS = 300

SYSTEM = """You split study material into coherent chunks for quiz generation.
Rules:
- Each chunk covers ONE concept: not too granular, not too broad.
- "content" must be copied VERBATIM from the source text (no rewording, no summarizing).
- "title" is a short label for the concept.
- Skip boilerplate (headers, page numbers, reference lists) — fewer good chunks beat filler.
- Return JSON: {"chunks": [{"title": ..., "content": ...}]}"""


def presplit(text: str, max_chars: int = MAX_SECTION_CHARS) -> list[str]:
    paras = [p for p in text.split("\n\n") if p.strip()]
    sections, current = [], ""
    for p in paras:
        if len(current) + len(p) + 2 > max_chars and current:
            sections.append(current)
            current = ""
        current = f"{current}\n\n{p}" if current else p
    if current.strip():
        sections.append(current)
    return sections or [text]


def _norm_with_map(s: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to single spaces; return normed + index map."""
    norm_chars, index_map = [], []
    prev_space = True  # strip leading whitespace
    for i, ch in enumerate(s):
        if ch.isspace():
            if not prev_space:
                norm_chars.append(" ")
                index_map.append(i)
            prev_space = True
        else:
            norm_chars.append(ch)
            index_map.append(i)
            prev_space = False
    if norm_chars and norm_chars[-1] == " ":  # strip trailing
        norm_chars.pop()
        index_map.pop()
    return "".join(norm_chars), index_map


def locate(content: str, full: str) -> tuple[int | None, int | None]:
    start = full.find(content)
    if start >= 0:
        return start, start + len(content)
    # fallback: whitespace-insensitive match, mapped back to original offsets
    needle = " ".join(content.split())
    if not needle:
        return None, None
    norm_full, index_map = _norm_with_map(full)
    at = norm_full.find(needle)
    if at < 0:
        return None, None
    start_orig = index_map[at]
    end_orig = index_map[at + len(needle) - 1] + 1
    return start_orig, end_orig


def chunk_sections(sections: list[str], llm: LLMClient) -> list[dict]:
    out = []
    for i, section in enumerate(sections):
        data = llm.complete_json(
            SYSTEM,
            f"Split the following study material (part {i + 1}/{len(sections)}):\n\n{section}",
        )
        for c in data.get("chunks", []):
            if c.get("content", "").strip():
                out.append({"title": c.get("title", "Untitled")[:200],
                            "content": c["content"]})
    return out


def chunk_resource(session: Session, resource, llm: LLMClient | None = None) -> int:
    """Chunk one extracted resource. Returns number of chunks created (0 if cached)."""
    from app.models import Resource  # noqa: F401 (type hint only)

    existing = session.exec(
        select(Chunk).where(Chunk.resource_id == resource.id)
    ).all()
    if existing and resource.status == "extracted":
        return 0  # cached
    for c in existing:  # content changed -> regenerate, don't version
        session.delete(c)
    session.commit()

    text = resource.extracted_text or ""
    items = [{"title": text[:80], "content": text}] if len(text) < MIN_LLM_CHARS else (
        chunk_sections(presplit(text), llm) if llm else []
    )
    for order, item in enumerate(items):
        start, end = locate(item["content"], text)
        session.add(
            Chunk(
                resource_id=resource.id, title=item["title"],
                content=item["content"], order=order,
                start_char=start, end_char=end,
            )
        )
    resource.status = "extracted" if items else "failed"
    if not items:
        resource.error = "chunker produced no chunks"
    session.add(resource)
    session.commit()
    return len(items)
