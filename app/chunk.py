"""LLM semantic chunking. One cheap-model call per section.

- Long docs (>MAX_SECTION_CHARS) are pre-split on paragraph boundaries
  before chunking, since chunk quality degrades on very long inputs. Text
  without blank lines (transcripts) falls back to lines, then spaces. A
  section whose reply is truncated is halved and retried.
- Short texts skip the LLM entirely (single chunk, no cost).
- Cache: resources that already have chunks and status "extracted" are
  skipped; status "pending" with stale chunks means content changed ->
  regenerate, swapping the old chunks out only once all sections succeeded
  (no versioning, per plan). Blank text is "skipped", never an empty chunk.
- Offsets are located locally with str.find (model ranges are unreliable),
  so Chunk.start_char/end_char support a future "show source" UI.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.llm import LLMClient, TruncatedError
from app.models import Chunk

MAX_SECTION_CHARS = 10000
MIN_LLM_CHARS = 300
MIN_SPLIT_CHARS = 1000  # a truncated reply on a shorter section is an error


class ChunkingError(RuntimeError):
    """The chunker returned no usable chunks; existing chunks are kept."""

SYSTEM = """You split study material into coherent chunks for quiz generation.
Rules:
- Each chunk covers ONE concept: not too granular, not too broad.
- "content" must be copied VERBATIM from the source text (no rewording, no summarizing).
- "title" is a short label for the concept.
- Skip boilerplate (headers, page numbers, reference lists) — fewer good chunks beat filler.
- Return JSON: {"chunks": [{"title": ..., "content": ...}]}"""


def presplit(text: str, max_chars: int = MAX_SECTION_CHARS,
             seps: tuple[str, ...] = ("\n\n", "\n", " ")) -> list[str]:
    """Sections of at most max_chars, packed from paragraphs; a piece still
    too long is split on the next separator, and as a last resort cut."""
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    if not seps:
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
    sep, rest = seps[0], seps[1:]
    sections, current = [], ""
    for part in text.split(sep):
        if not part.strip():
            continue
        for piece in presplit(part, max_chars, rest):
            if current and len(current) + len(sep) + len(piece) > max_chars:
                sections.append(current)
                current = ""
            current = f"{current}{sep}{piece}" if current else piece
    if current.strip():
        sections.append(current)
    return sections


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


def _chunks_of(data: dict) -> list[dict]:
    """Usable chunks from a reply; malformed entries are dropped."""
    raw = data["chunks"] if isinstance(data["chunks"], list) else []
    out = []
    for c in raw:
        if not isinstance(c, dict) or not isinstance(c.get("content"), str):
            continue
        if not c["content"].strip():
            continue
        title = c.get("title")
        title = title.strip() if isinstance(title, str) and title.strip() else "Untitled"
        out.append({"title": title[:200], "content": c["content"]})
    return out


def chunk_sections(sections: list[str], llm: LLMClient) -> list[dict]:
    out = []
    for i, section in enumerate(sections):
        try:
            data = llm.complete_json(
                SYSTEM,
                f"Split the following study material (part {i + 1}/{len(sections)}):"
                f"\n\n{section}",
                required_key="chunks",
            )
        except TruncatedError:
            if len(section) < MIN_SPLIT_CHARS:
                raise
            # the reply outgrew the output limit: two halves fit
            out.extend(chunk_sections(presplit(section, len(section) // 2 + 1), llm))
            continue
        out.extend(_chunks_of(data))
    return out


def needs_llm(resource) -> bool:
    return len(resource.extracted_text or "") >= MIN_LLM_CHARS


def chunk_resource(session: Session, resource, llm: LLMClient | None = None) -> int:
    """Chunk one extracted resource. Returns number of chunks created.

    Returns 0 without touching anything when cached, or when the text is long
    enough to need an LLM and none was given (extract-only runs must not mark
    resources failed or drop their existing chunks). LLM errors and an empty
    result (ChunkingError) raise before any existing chunk is touched.
    """
    from app.models import Resource  # noqa: F401 (type hint only)

    existing = session.exec(
        select(Chunk).where(Chunk.resource_id == resource.id)
    ).all()
    if existing and resource.status == "extracted":
        return 0  # cached
    if llm is None and needs_llm(resource):
        return 0  # leave as-is for a run that has an LLM

    text = resource.extracted_text or ""
    if not text.strip():
        items = []
    elif needs_llm(resource):
        items = chunk_sections(presplit(text), llm)  # paid work: before any delete
        if not items:
            raise ChunkingError("chunker produced no chunks")
    else:
        items = [{"title": text.strip()[:80], "content": text}]
    for c in existing:  # content changed -> regenerate, don't version
        session.delete(c)
    for order, item in enumerate(items):
        start, end = locate(item["content"], text)
        session.add(
            Chunk(
                resource_id=resource.id, title=item["title"],
                content=item["content"], order=order,
                start_char=start, end_char=end,
            )
        )
    resource.status = "extracted" if items else "skipped"
    resource.error = None if items else "no text to chunk"
    session.add(resource)
    session.commit()  # old chunks out, new ones in: one transaction
    return len(items)
