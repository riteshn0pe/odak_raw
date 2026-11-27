"""Parser selection by content, not by filename.

Extensions lie and clients send application/octet-stream. Magic bytes are checked
first and the extension is only a tiebreak, because a mislabelled file that
parses as the wrong format produces silent garbage in the index.
"""

from app.core.errors import UnsupportedFormatError
from app.core.logging import get_logger
from app.domain.ports import DocumentParser

logger = get_logger(__name__)

EXTENSION_MIMES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "html": "text/html",
    "htm": "text/html",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "log": "text/plain",
}

# ooxml formats are all zip archives, so the zip signature alone cannot tell
# docx from xlsx. the archive member names can
OOXML_MARKERS = {
    "word/": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xl/": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt/": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def sniff_mime(data: bytes, filename: str) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"

    if data.startswith(b"PK\x03\x04"):
        try:
            import zipfile
            from io import BytesIO

            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = archive.namelist()
            for marker, mime in OOXML_MARKERS.items():
                if any(name.startswith(marker) for name in names):
                    return mime
        except Exception:
            pass
        raise UnsupportedFormatError("zip archive", "unpack it and upload the files individually")

    head = data[:2048].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return "text/html"

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in EXTENSION_MIMES:
        return EXTENSION_MIMES[extension]

    try:
        data[:4096].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        raise UnsupportedFormatError(extension or "unknown", "convert to PDF, DOCX or text") from None


class ParserRegistry:
    def __init__(self) -> None:
        from app.adapters.parsers.office import (
            DocxParser,
            HtmlParser,
            PptxParser,
            TextParser,
            XlsxParser,
        )
        from app.adapters.parsers.pdf import PdfParser

        self._parsers: list[DocumentParser] = [
            PdfParser(),
            DocxParser(),
            XlsxParser(),
            PptxParser(),
            HtmlParser(),
            TextParser(),
        ]
        self._by_mime: dict[str, DocumentParser] = {}
        for parser in self._parsers:
            for mime in parser.supported_mimes:
                self._by_mime[mime] = parser

    def resolve(self, data: bytes, filename: str) -> tuple[DocumentParser, str]:
        mime = sniff_mime(data, filename)
        parser = self._by_mime.get(mime)
        if parser is None:
            raise UnsupportedFormatError(mime, "supported: PDF, DOCX, XLSX, PPTX, HTML, text, CSV, JSON")
        return parser, mime

    def supported_mimes(self) -> list[str]:
        return sorted(self._by_mime)
