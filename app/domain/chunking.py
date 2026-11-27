"""Chunk boundary logic and deterministic id derivation.

Pure functions, no io, so this is unit testable without a database or a provider.

Two strategies, chosen by input shape:
  structured    split on markdown heading hierarchy, tables stay whole
  parent_child  small child embedded for precision, larger parent carried for context

Ids are derived from tenant, document, ordinal and kind. Re-running ingestion for
the same document produces the same ids, so a retry overwrites instead of
duplicating. That is what makes the pipeline safe to replay.
"""

import hashlib
import re
from collections.abc import Iterator
from uuid import UUID

from app.core.constants import (
    CHILD_CHUNK_OVERLAP,
    CHILD_CHUNK_SIZE,
    MAX_STRUCTURED_CHUNK_SIZE,
    PARENT_CHUNK_OVERLAP,
    PARENT_CHUNK_SIZE,
    STRUCTURED_CHUNK_OVERLAP,
    ChunkKind,
)
from app.domain.entities import Chunk

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
SPLIT_SEPARATORS = ("\n\n", "\n", ". ", " ")


def derive_chunk_id(
    tenant_id: UUID, document_id: UUID, ordinal: int, kind: ChunkKind, sub: int = 0
) -> str:
    raw = f"{tenant_id}:{document_id}:{kind.value}:{ordinal}:{sub}"
    return hashlib.sha256(raw.encode()).hexdigest()


def derive_parent_id(tenant_id: UUID, document_id: UUID, parent_ordinal: int) -> str:
    raw = f"{tenant_id}:{document_id}:parent:{parent_ordinal}"
    return hashlib.sha256(raw.encode()).hexdigest()


def estimate_tokens(text: str) -> int:
    """Cheap heuristic. Real counting happens in the provider adapter that bills."""
    return max(1, len(text) // 4)


def split_by_size(text: str, size: int, overlap: int) -> list[str]:
    """Recursive separator split. Prefers paragraph, then line, then sentence.

    Never returns an empty piece and never loses characters between pieces.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = -1
            for separator in SPLIT_SEPARATORS:
                found = text.rfind(separator, start + size // 2, end)
                if found > cut:
                    cut = found + len(separator)
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return pieces


def _table_blocks(text: str) -> list[tuple[int, int]]:
    """Line ranges that form markdown tables. These are never split."""
    lines = text.split("\n")
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        if TABLE_LINE_RE.match(line):
            if start is None:
                start = index
        elif start is not None:
            if index - start >= 2:
                blocks.append((start, index))
            start = None
    if start is not None and len(lines) - start >= 2:
        blocks.append((start, len(lines)))
    return blocks


def split_structured(markdown: str, max_size: int = MAX_STRUCTURED_CHUNK_SIZE) -> list[
    tuple[list[str], str]
]:
    """Split on heading hierarchy. Returns (heading_path, section_text) pairs.

    A section larger than max_size is split further but keeps its heading path, so
    a fragment never loses the context of which section it came from. Tables are
    kept intact even when that pushes a piece past max_size, because a table split
    from its header row is worse than a long chunk.
    """
    matches = list(HEADING_RE.finditer(markdown))
    sections: list[tuple[list[str], str]] = []

    if not matches:
        for piece in split_by_size(markdown, max_size, STRUCTURED_CHUNK_OVERLAP):
            sections.append(([], piece))
        return sections

    # text before the first heading still belongs to the document
    preamble = markdown[: matches[0].start()].strip()
    if preamble:
        for piece in split_by_size(preamble, max_size, STRUCTURED_CHUNK_OVERLAP):
            sections.append(([], piece))

    path: list[str] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        path = path[: level - 1]
        while len(path) < level - 1:
            path.append("")
        path.append(title)

        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.start() : end].strip()
        if not body:
            continue

        current_path = [part for part in path if part]
        if len(body) <= max_size:
            sections.append((current_path, body))
            continue

        for piece in _split_preserving_tables(body, max_size):
            sections.append((current_path, piece))

    return sections


def _split_preserving_tables(text: str, max_size: int) -> list[str]:
    blocks = _table_blocks(text)
    if not blocks:
        return split_by_size(text, max_size, STRUCTURED_CHUNK_OVERLAP)

    lines = text.split("\n")
    protected = {index for start, end in blocks for index in range(start, end)}

    pieces: list[str] = []
    buffer: list[str] = []
    buffer_len = 0

    def flush() -> None:
        nonlocal buffer, buffer_len
        if buffer:
            joined = "\n".join(buffer).strip()
            if joined:
                pieces.append(joined)
        buffer = []
        buffer_len = 0

    index = 0
    while index < len(lines):
        if index in protected:
            block = next(b for b in blocks if b[0] <= index < b[1])
            table = "\n".join(lines[block[0] : block[1]])
            if buffer_len + len(table) > max_size:
                flush()
            buffer.append(table)
            buffer_len += len(table)
            index = block[1]
            continue

        line = lines[index]
        if buffer_len + len(line) > max_size:
            flush()
        buffer.append(line)
        buffer_len += len(line) + 1
        index += 1

    flush()
    return pieces


def is_heading_only(body: str) -> bool:
    """True when a section contains headings and nothing else.

    A bare title is a label, not an answer. Left as its own chunk it competes in
    retrieval, wins on brevity, and returns no information to the model.
    """
    lines = [line.strip() for line in body.split("\n") if line.strip()]
    return bool(lines) and all(line.startswith("#") for line in lines)


def merge_heading_only(
    sections: list[tuple[list[str], str]],
) -> list[tuple[list[str], str]]:
    """Fold a heading only section into the section that follows it.

    The heading still reaches the index, attached to the content it introduces,
    where it adds retrievable signal instead of noise.
    """
    merged: list[tuple[list[str], str]] = []
    pending: list[str] = []

    for heading_path, body in sections:
        if is_heading_only(body):
            pending.append(body)
            continue
        if pending:
            body = "\n\n".join([*pending, body])
            pending = []
        merged.append((heading_path, body))

    # trailing headings with no content still belong in the document
    if pending:
        if merged:
            path, body = merged[-1]
            merged[-1] = (path, "\n\n".join([body, *pending]))
        else:
            merged.append(([], "\n\n".join(pending)))
    return merged


def build_structured_chunks(
    tenant_id: UUID,
    document_id: UUID,
    markdown: str,
    page_of_offset: dict[int, int] | None = None,
    max_size: int = MAX_STRUCTURED_CHUNK_SIZE,
) -> list[Chunk]:
    sections = merge_heading_only(split_structured(markdown, max_size))
    chunks: list[Chunk] = []
    for ordinal, (heading_path, body) in enumerate(sections):
        chunks.append(
            Chunk(
                id=derive_chunk_id(tenant_id, document_id, ordinal, ChunkKind.STRUCTURED),
                tenant_id=tenant_id,
                document_id=document_id,
                ordinal=ordinal,
                kind=ChunkKind.STRUCTURED,
                text=body,
                parent_text=None,
                heading_path=heading_path,
                token_count=estimate_tokens(body),
            )
        )
    return chunks


def build_parent_child_chunks(
    tenant_id: UUID,
    document_id: UUID,
    text: str,
    parent_size: int = PARENT_CHUNK_SIZE,
    parent_overlap: int = PARENT_CHUNK_OVERLAP,
    child_size: int = CHILD_CHUNK_SIZE,
    child_overlap: int = CHILD_CHUNK_OVERLAP,
) -> list[Chunk]:
    """Child chunks are embedded, parent text rides along for generation context.

    Every child carries parent_id so retrieval can collapse siblings. The old
    project only set parent_id on one strategy, so markdown chunks never deduped.
    """
    chunks: list[Chunk] = []
    ordinal = 0

    for parent_ordinal, parent_text in enumerate(
        split_by_size(text, parent_size, parent_overlap)
    ):
        parent_id = derive_parent_id(tenant_id, document_id, parent_ordinal)
        for child_text in split_by_size(parent_text, child_size, child_overlap):
            chunks.append(
                Chunk(
                    id=derive_chunk_id(
                        tenant_id, document_id, ordinal, ChunkKind.PARENT_CHILD
                    ),
                    tenant_id=tenant_id,
                    document_id=document_id,
                    ordinal=ordinal,
                    kind=ChunkKind.PARENT_CHILD,
                    text=child_text,
                    parent_text=parent_text,
                    parent_id=parent_id,
                    token_count=estimate_tokens(child_text),
                )
            )
            ordinal += 1
    return chunks


def build_tabular_chunks(
    tenant_id: UUID,
    document_id: UUID,
    header: list[str],
    rows: list[list[str]],
    rows_per_chunk: int = 20,
) -> list[Chunk]:
    """Row groups with the header repeated in every chunk.

    A detached data row is meaningless to an embedding, so the header is not
    optional here.
    """
    chunks: list[Chunk] = []
    header_line = " | ".join(header)
    separator = " | ".join("---" for _ in header)

    for ordinal, start in enumerate(range(0, len(rows), rows_per_chunk)):
        window = rows[start : start + rows_per_chunk]
        body = "\n".join(" | ".join(str(cell) for cell in row) for row in window)
        text = f"{header_line}\n{separator}\n{body}"
        chunks.append(
            Chunk(
                id=derive_chunk_id(tenant_id, document_id, ordinal, ChunkKind.TABULAR),
                tenant_id=tenant_id,
                document_id=document_id,
                ordinal=ordinal,
                kind=ChunkKind.TABULAR,
                text=text,
                heading_path=[],
                token_count=estimate_tokens(text),
                metadata={"row_from": start, "row_to": start + len(window)},
            )
        )
    return chunks


def build_qa_chunks(
    tenant_id: UUID,
    document_id: UUID,
    title: str,
    questions: list[str],
    answer: str,
) -> list[Chunk]:
    """One vector per question phrasing, all carrying the answer as their text.

    A support question is asked in the user's words, not the answer's, so the
    vector that has to match is the question. `embed_text` carries the question
    into the embedder while `text` stays the answer, which is what the model
    reads and what a citation quotes — the field pair already exists for the
    contextualiser and is exactly this shape.

    A final chunk embeds the answer itself, so a user who phrases their
    question in the answer's vocabulary (or copies an exact term out of it,
    which is what the sparse half of hybrid search is for) still lands.

    Every chunk shares one parent_id so retrieval collapses three matching
    phrasings of one pair into a single hit instead of monopolising the context
    window.
    """
    parent_id = derive_parent_id(tenant_id, document_id, 0)
    chunks: list[Chunk] = []
    variations: list[str | None] = [*questions, None]
    for ordinal, question in enumerate(variations):
        chunks.append(
            Chunk(
                id=derive_chunk_id(tenant_id, document_id, ordinal, ChunkKind.QA),
                tenant_id=tenant_id,
                document_id=document_id,
                ordinal=ordinal,
                kind=ChunkKind.QA,
                text=answer,
                parent_text=answer,
                parent_id=parent_id,
                # None on the last chunk, so text_to_embed falls back to the
                # answer and the pair is reachable from both directions
                embed_text=question,
                heading_path=[title],
                token_count=estimate_tokens(answer),
                metadata={"question": question} if question else {"question": None},
            )
        )
    return chunks


def dedupe_key(chunk_id: str, parent_id: str | None, kind: ChunkKind) -> str:
    """Collapse key for retrieval.

    Parent child collapses to the parent so sibling children do not crowd out
    other documents. Structured and tabular chunks are already whole units and
    collapse to themselves.
    """
    # a Q&A pair's question variations collapse to the pair, for the same
    # reason parent_child children collapse to their parent: several phrasings
    # of one answer must occupy one slot, not five
    if kind in (ChunkKind.PARENT_CHILD, ChunkKind.QA) and parent_id:
        return parent_id
    return chunk_id


def iter_batches(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
