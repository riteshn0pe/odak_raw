"""PDF parsing with a tiered ladder.

Cost per page differs by orders of magnitude between tiers, so pages are routed
individually rather than whole documents. A 200 page PDF with 3 scanned pages
should pay for 3 OCR pages, not 200.

  fast   PyMuPDF text extraction, effectively free, works on digital PDFs
  ocr    rasterise then OCR, for scanned pages with no text layer
  vlm    vision model, for pages whose value is layout: dense tables, forms

Routing looks at what the page actually contains, not the file extension.
"""

import asyncio
import re
from typing import Any

import fitz
from app.core.constants import (
    OCR_MIN_CHARS_PER_PAGE,
    OCR_RASTER_DPI,
    TABLE_AREA_RATIO_FOR_VLM,
    ParseTier,
)
from app.core.logging import get_logger
from app.domain.entities import ParsedDocument, ParsedPage

logger = get_logger(__name__)

WHITESPACE_RE = re.compile(r"[ \t]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")


def normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("­", "")  # soft hyphen, an artefact of justified text
    text = WHITESPACE_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def classify_page(page: "fitz.Page") -> tuple[ParseTier, list[str]]:
    """Decide the cheapest tier that will produce usable text for this page."""
    warnings: list[str] = []
    text = page.get_text("text").strip()
    char_count = len(text)

    if char_count < OCR_MIN_CHARS_PER_PAGE:
        images = page.get_images(full=True)
        if images:
            return ParseTier.OCR, ["no text layer, image present"]
        # genuinely blank, do not pay for ocr on an empty page
        return ParseTier.FAST, ["page appears blank"]

    try:
        tables = page.find_tables()
        table_count = len(tables.tables)
    except Exception:  # pragma: no cover - pymupdf table finder is best effort
        table_count = 0
        warnings.append("table detection failed")

    if table_count:
        page_area = abs(page.rect.get_area()) or 1.0
        table_area = 0.0
        for table in page.find_tables().tables:
            rect = fitz.Rect(table.bbox)
            table_area += abs(rect.get_area())
        if table_area / page_area > TABLE_AREA_RATIO_FOR_VLM:
            return ParseTier.VLM, [f"{table_count} table(s) cover most of the page"]

    return ParseTier.FAST, warnings


def extract_tables_markdown(page: "fitz.Page") -> list[str]:
    """Tables as markdown so structure survives chunking.

    A table flattened into prose loses the row and column relationship, which is
    usually the only reason the table was in the document.
    """
    blocks: list[str] = []
    try:
        found = page.find_tables()
    except Exception:
        return blocks

    for table in found.tables:
        try:
            rows = table.extract()
        except Exception:
            continue
        if not rows:
            continue

        cleaned = [
            [normalise(str(cell)) if cell is not None else "" for cell in row] for row in rows
        ]
        width = max(len(row) for row in cleaned)
        cleaned = [row + [""] * (width - len(row)) for row in cleaned]

        header, *body = cleaned
        if not any(header):
            header = [f"col{index + 1}" for index in range(width)]
            body = cleaned

        lines = [
            "| " + " | ".join(cell.replace("|", "\\|") for cell in header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        lines.extend(
            "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in body
        )
        blocks.append("\n".join(lines))
    return blocks


def _heading_level(size: float, body_size: float) -> int | None:
    if body_size <= 0:
        return None
    ratio = size / body_size
    if ratio >= 1.5:
        return 1
    if ratio >= 1.3:
        return 2
    if ratio >= 1.15:
        return 3
    return None


def page_to_markdown(page: "fitz.Page", body_size: float) -> str:
    """Reconstruct markdown headings from font size.

    Heading structure is what makes chunk boundaries meaningful, and PDFs carry
    no semantic headings, only visual ones. Relative font size is the signal.
    """
    data: dict[str, Any] = page.get_text("dict")
    parts: list[str] = []

    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = normalise("".join(span.get("text", "") for span in spans))
            if not text:
                continue

            max_size = max(float(span.get("size", 0.0)) for span in spans)
            bold = any("bold" in str(span.get("font", "")).lower() for span in spans)
            level = _heading_level(max_size, body_size)

            if level and len(text) < 200:
                parts.append(f"{'#' * level} {text}")
            elif bold and len(text) < 120 and text.endswith(":"):
                parts.append(f"### {text.rstrip(':')}")
            else:
                parts.append(text)

    markdown = "\n".join(parts)
    for table in extract_tables_markdown(page):
        markdown += "\n\n" + table
    return normalise(markdown)


def body_font_size(document: "fitz.Document", sample_pages: int = 5) -> float:
    """Modal font size across a sample. Body text is whatever appears most."""
    counts: dict[int, int] = {}
    for index in range(min(sample_pages, document.page_count)):
        data: dict[str, Any] = document[index].get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    size = round(float(span.get("size", 0.0)))
                    if size:
                        counts[size] = counts.get(size, 0) + len(span.get("text", ""))
    if not counts:
        return 11.0
    return float(max(counts.items(), key=lambda item: item[1])[0])


def render_page(page: "fitz.Page", dpi: int = OCR_RASTER_DPI) -> bytes:
    """Rasterise one page for an OCR provider.

    Resolution is a direct cost lever: too low loses small type, too high inflates
    the image-token bill on every escalated page without improving recovery.
    """
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    return pixmap.tobytes("png")  # type: ignore[no-any-return]


class PdfParser:
    """Tier one PDF parser. Emits per page routing decisions so the caller can
    escalate only the pages that need it."""

    supported_mimes = ("application/pdf",)

    async def parse(self, data: bytes, filename: str) -> ParsedDocument:
        # the gate rejects encrypted pdfs at upload, but the reindex and crawl
        # paths reparse stored bytes with no gate, so the check has to live here
        # too. pymupdf raises a bare ValueError on page access once encrypted,
        # which would surface as internal_error rather than an actionable reason
        try:
            document = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            from app.core.errors import ParseError

            raise ParseError(
                "the file could not be opened as a pdf", filename=filename
            ) from exc

        if document.is_encrypted and not document.authenticate(""):
            document.close()
            from app.core.errors import EncryptedDocumentError

            raise EncryptedDocumentError("remove the password and upload again")

        # text extraction and rasterisation are cpu bound and take seconds on a
        # large document; running them inline would freeze the event loop for
        # every other tenant on the worker. close stays inside the helper, which
        # owns the document from here on
        return await asyncio.to_thread(self._parse_document, document, filename)

    def _parse_document(self, document: "fitz.Document", filename: str) -> ParsedDocument:
        try:
            body_size = body_font_size(document)
            pages: list[ParsedPage] = []

            for index in range(document.page_count):
                page = document[index]
                tier, warnings = classify_page(page)
                markdown = page_to_markdown(page, body_size)
                escalating = tier in (ParseTier.OCR, ParseTier.VLM)

                # rasterise only what will actually be sent onward. rendering
                # every page would cost memory on documents that never escalate
                image: bytes | None = None
                if escalating:
                    try:
                        image = render_page(page)
                    except Exception:
                        logger.warning(
                            "page_render_failed", page=index + 1, exc_info=True
                        )
                        warnings.append("could not rasterise this page for OCR")

                # escalation is the caller's job. record the need and carry
                # whatever text we did get so a failed escalation still has text
                pages.append(
                    ParsedPage(
                        page_number=index + 1,
                        markdown=markdown,
                        tier=tier,
                        degraded=escalating,
                        warnings=warnings,
                        image=image,
                        needs_escalation=escalating and image is not None,
                    )
                )

            metadata = {
                "title": document.metadata.get("title") or filename,
                "author": document.metadata.get("author") or "",
                "page_count": document.page_count,
                "encrypted": document.is_encrypted,
            }
            return ParsedDocument(pages=pages, metadata=metadata)
        finally:
            document.close()


def needs_escalation(parsed: ParsedDocument) -> list[int]:
    """Page numbers a cheap pass could not handle and that have a raster to send."""
    return [page.page_number for page in parsed.pages if page.needs_escalation]


def drop_rasters(parsed: ParsedDocument) -> None:
    """Release page images once escalation is done.

    A raster is tens of times larger than the text it produced, so holding them
    past this point is the difference between a large document fitting in memory
    and not.
    """
    for page in parsed.pages:
        page.image = None
