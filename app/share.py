"""Reuse work another user's copy of the same material already paid for.

Every enrolled user syncs their own Course/Topic/Resource rows, so without
this each one pays the download, the chunker call and the quiz calls again.
Rows stay per user (review states, purges and retirement are untouched); the
pipeline copies a donor's results instead of redoing them:

- extraction: a donor with the same (source, type, content_hash) whose text
  is extracted. Hashes are the source's change markers (app.sync), so equal
  hashes mean the same file the user's own source listed to them.
- chunks: a donor with byte-identical extracted_text that is chunked, so the
  copied offsets are valid in the user's text.
- quiz items: a donor chunk with identical content, under a resource with the
  same hash, that has a QuizAttempt for the attempt (copied even when it
  yielded no items, so nothing-quizzable chunks aren't re-billed either).

Chunks and quiz items are only ever copied from text the user already has, so
sharing them can't reveal anything new.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import Chunk, QuizAttempt, QuizItem, Resource


def _same_material(r: Resource):
    return select(Resource).where(
        Resource.content_hash == r.content_hash, Resource.source == r.source,
        Resource.type == r.type, Resource.id != r.id,
    ).order_by(Resource.id)


def copy_extraction(session: Session, r: Resource) -> bool:
    """Take a donor's extracted text; True when one was found. Uncommitted."""
    if r.content_hash is None:
        return False
    for donor in session.exec(_same_material(r).where(
        Resource.status == "extracted", Resource.extracted_text.is_not(None),
    )):
        if donor.extracted_text.strip():
            r.extracted_text = donor.extracted_text
            r.status = "extracted"
            r.error = None
            return True
    return False


def copy_chunks(session: Session, r: Resource) -> int:
    """Copy a chunked donor's chunks onto r, replacing what r has; returns the
    number copied (0 = no donor, nothing touched). Commits."""
    from app.sync import _purge_derived

    if r.content_hash is None or not (r.extracted_text or "").strip():
        return 0
    has_chunks = select(Chunk.id).where(Chunk.resource_id == Resource.id).exists()
    donor = session.exec(_same_material(r).where(
        Resource.status == "extracted", Resource.extracted_text == r.extracted_text,
        has_chunks,
    )).first()
    if donor is None:
        return 0
    chunks = session.exec(
        select(Chunk).where(Chunk.resource_id == donor.id).order_by(Chunk.order)
    ).all()
    _purge_derived(session, r.id)  # stale chunks from older content, if any
    for c in chunks:
        session.add(Chunk(resource_id=r.id, title=c.title, content=c.content,
                          order=c.order, start_char=c.start_char, end_char=c.end_char))
    r.status = "extracted"
    r.error = None
    session.add(r)
    session.commit()
    return len(chunks)


def copy_quiz(session: Session, chunk: Chunk, attempt: int = 1) -> list[QuizItem] | None:
    """Copy a donor chunk's quiz items for `attempt`; None when there is no
    donor, else the copied items (possibly none). Commits."""
    r = session.get(Resource, chunk.resource_id)
    if r is None or r.content_hash is None:
        return None
    tried = select(QuizAttempt.chunk_id).where(
        QuizAttempt.chunk_id == Chunk.id, QuizAttempt.attempt == attempt
    ).exists()
    donor = session.exec(
        select(Chunk).join(Resource, Resource.id == Chunk.resource_id).where(
            Resource.content_hash == r.content_hash, Resource.source == r.source,
            Chunk.id != chunk.id, Chunk.content == chunk.content, tried,
        ).order_by(Chunk.id)
    ).first()
    if donor is None:
        return None
    donor_key, key = f"{donor.id}:{attempt}", f"{chunk.id}:{attempt}"
    created = []
    for item in session.exec(
        select(QuizItem).where(QuizItem.chunk_id == donor.id).order_by(QuizItem.generation_key)
    ):
        if item.generation_key != donor_key and not item.generation_key.startswith(
                donor_key + ":"):
            continue  # another attempt's items
        row = QuizItem(
            chunk_id=chunk.id, question=item.question, question_type=item.question_type,
            options=item.options, correct_answer=item.correct_answer,
            grading_criteria=item.grading_criteria, explanation=item.explanation,
            difficulty=item.difficulty,
            generation_key=key + item.generation_key[len(donor_key):],
        )
        session.add(row)
        created.append(row)
    session.add(QuizAttempt(chunk_id=chunk.id, attempt=attempt))
    session.commit()
    return created
