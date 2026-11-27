"""Office and text format parsers.

These formats carry real structure, so extraction is deterministic and cheap. No
tiering needed: DOCX heading styles, XLSX sheets and HTML tags map to markdown
directly.
"""

import csv
import io
import json
from typing import Any

from app.core.constants import ParseTier
from app.core.logging import get_logger
from app.domain.entities import ParsedDocument, ParsedPage

logger = get_logger(__name__)


def _page(markdown: str, number: int = 1, warnings: list[str] | None = None) -> ParsedPage:
    return ParsedPage(
        page_number=number,
        markdown=markdown.strip(),
        tier=ParseTier.FAST,
        warnings=warnings or [],
    )


class DocxParser:
    supported_mimes = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    async def parse(self, data: bytes, filename: str) -> ParsedDocument:
        import docx

        document = docx.Document(io.BytesIO(data))
        parts: list[str] = []

        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower()
            if style.startswith("heading"):
                level = "".join(char for char in style if char.isdigit()) or "1"
                parts.append(f"{'#' * min(int(level), 6)} {text}")
            elif style.startswith("title"):
                parts.append(f"# {text}")
            elif style.startswith("list"):
                parts.append(f"- {text}")
            else:
                parts.append(text)

        for table in document.tables:
            rows = [[cell.text.strip().replace("|", "\\|") for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            header, *body = rows
            parts.append("")
            parts.append("| " + " | ".join(header) + " |")
            parts.append("| " + " | ".join("---" for _ in header) + " |")
            parts.extend("| " + " | ".join(row) + " |" for row in body)

        return ParsedDocument(
            pages=[_page("\n\n".join(parts))],
            metadata={"title": filename, "paragraphs": len(document.paragraphs)},
        )


class XlsxParser:
    """Each sheet becomes one page of markdown table.

    Row order and column headers are the meaning of a spreadsheet, so they are
    preserved verbatim rather than summarised.
    """

    supported_mimes = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)
    MAX_ROWS_PER_SHEET = 2000

    async def parse(self, data: bytes, filename: str) -> ParsedDocument:
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        pages: list[ParsedPage] = []

        for index, sheet in enumerate(workbook.worksheets, start=1):
            rows: list[list[str]] = []
            truncated = False
            for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                if row_index >= self.MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
                cells = ["" if value is None else str(value).strip() for value in row]
                if any(cells):
                    rows.append(cells)

            if not rows:
                continue

            width = max(len(row) for row in rows)
            rows = [row + [""] * (width - len(row)) for row in rows]
            header, *body = rows

            lines = [f"## {sheet.title}", ""]
            lines.append("| " + " | ".join(cell.replace("|", "\\|") or f"col{i+1}" for i, cell in enumerate(header)) + " |")
            lines.append("| " + " | ".join("---" for _ in header) + " |")
            lines.extend(
                "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in body
            )

            warnings = [f"truncated at {self.MAX_ROWS_PER_SHEET} rows"] if truncated else []
            pages.append(_page("\n".join(lines), number=index, warnings=warnings))

        workbook.close()
        return ParsedDocument(pages=pages, metadata={"title": filename, "sheets": len(pages)})


class PptxParser:
    supported_mimes = (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )

    async def parse(self, data: bytes, filename: str) -> ParsedDocument:
        from pptx import Presentation

        presentation = Presentation(io.BytesIO(data))
        pages: list[ParsedPage] = []

        for index, slide in enumerate(presentation.slides, start=1):
            parts: list[str] = []
            title = getattr(slide.shapes, "title", None)
            if title is not None and title.has_text_frame and title.text.strip():
                parts.append(f"## {title.text.strip()}")

            for shape in slide.shapes:
                if shape is title:
                    continue
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = "".join(run.text for run in paragraph.runs).strip()
                        if text:
                            prefix = "  " * max(paragraph.level, 0)
                            parts.append(f"{prefix}- {text}")
                elif shape.has_table:
                    rows = [
                        [cell.text.strip().replace("|", "\\|") for cell in row.cells]
                        for row in shape.table.rows
                    ]
                    if rows:
                        header, *body = rows
                        parts.append("| " + " | ".join(header) + " |")
                        parts.append("| " + " | ".join("---" for _ in header) + " |")
                        parts.extend("| " + " | ".join(row) + " |" for row in body)

            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                parts.append(f"\n_Speaker notes: {slide.notes_slide.notes_text_frame.text.strip()}_")

            if parts:
                pages.append(_page("\n".join(parts), number=index))

        return ParsedDocument(pages=pages, metadata={"title": filename, "slides": len(pages)})


class HtmlParser:
    supported_mimes = ("text/html", "application/xhtml+xml")

    async def parse(self, data: bytes, filename: str) -> ParsedDocument:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
            tag.decompose()

        parts: list[str] = []
        for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table"]):
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            name = element.name
            if name.startswith("h") and len(name) == 2 and name[1].isdigit():
                parts.append(f"{'#' * int(name[1])} {text}")
            elif name == "li":
                parts.append(f"- {text}")
            elif name == "pre":
                parts.append(f"```\n{element.get_text()}\n```")
            elif name == "table":
                rows = [
                    [cell.get_text(" ", strip=True).replace("|", "\\|") for cell in row.find_all(["td", "th"])]
                    for row in element.find_all("tr")
                ]
                rows = [row for row in rows if row]
                if rows:
                    header, *body = rows
                    parts.append("| " + " | ".join(header) + " |")
                    parts.append("| " + " | ".join("---" for _ in header) + " |")
                    parts.extend("| " + " | ".join(row) + " |" for row in body)
            else:
                parts.append(text)

        title = soup.title.string.strip() if soup.title and soup.title.string else filename
        return ParsedDocument(pages=[_page("\n\n".join(parts))], metadata={"title": title})


class TextParser:
    """Plain text, markdown, CSV and JSON.

    Markdown passes through untouched since it is already the target format.
    """

    supported_mimes = ("text/plain", "text/markdown", "text/csv", "application/json")

    async def parse(self, data: bytes, filename: str) -> ParsedDocument:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")

        lower = filename.lower()
        if lower.endswith(".csv"):
            text = self._csv_to_markdown(text)
        elif lower.endswith(".json"):
            text = self._json_to_markdown(text)

        return ParsedDocument(pages=[_page(text)], metadata={"title": filename})

    def _csv_to_markdown(self, text: str) -> str:
        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader if any(cell.strip() for cell in row)]
        if not rows:
            return text
        header, *body = rows
        lines = [
            "| " + " | ".join(cell.strip().replace("|", "\\|") for cell in header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        lines.extend(
            "| " + " | ".join(cell.strip().replace("|", "\\|") for cell in row) + " |" for row in body
        )
        return "\n".join(lines)

    def _json_to_markdown(self, text: str) -> str:
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            return text
        return f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```"
