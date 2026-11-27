"""Meta WhatsApp Cloud API adapter and WhatsApp-safe text formatting."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from typing import cast

import httpx

from app.core.breaker import guard
from app.core.errors import ProviderError, ProviderUnavailableError

META_GRAPH_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"
DEFAULT_MAX_MESSAGE_LENGTH = 3500
META_MAX_MESSAGE_LENGTH = 4096
_PROVIDER = "meta_whatsapp"

type JSONValue = str | int | float | bool | list["JSONValue"] | dict[str, "JSONValue"] | None

_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*```(?:[\w.+-]+)?\s*$")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_IMAGE = re.compile(r"!\[([^]]*)]\([^)]*\)")
_LINK = re.compile(r"\[([^]]+)]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_STRIKETHROUGH = re.compile(r"~~(.+?)~~")
_BOLD = re.compile(r"\*\*(?=\S)(.+?\S)\*\*|__(?=\S)(.+?\S)__")
_ITALIC_ASTERISK = re.compile(r"(?<!\*)\*(?=\S)(.+?\S)\*(?!\*)")
_MARKDOWN_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_HORIZONTAL_RULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$")
_UNORDERED_LIST = re.compile(r"^(\s*)[-+]\s+", re.MULTILINE)
_ORDERED_LIST = re.compile(r"^(\s*)\d+[.)]\s+", re.MULTILINE)


def verify_meta_signature(raw: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` value in constant time."""

    if not signature_header or not app_secret:
        return False
    prefix, separator, supplied_hex = signature_header.strip().partition("=")
    valid_length = hashlib.sha256().digest_size * 2
    if separator != "=" or prefix != "sha256" or len(supplied_hex) != valid_length:
        return False
    try:
        supplied = bytes.fromhex(supplied_hex)
    except ValueError:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return hmac.compare_digest(expected, supplied)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return len(cells) >= 2 and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells)


def _format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    formatted: list[str] = []
    for row in rows:
        fields: list[str] = []
        for index, value in enumerate(row):
            if not value:
                continue
            header = headers[index] if index < len(headers) else ""
            fields.append(f"**{header}:** {value}" if header else value)
        if fields:
            formatted.append("• " + " | ".join(fields))
    return formatted


def _convert_tables(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and "|" in lines[index] and _is_table_separator(lines[index + 1]):
            headers = _split_table_row(lines[index])
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_table_row(lines[index]))
                index += 1
            output.extend(_format_table(headers, rows))
            continue
        output.append(lines[index])
        index += 1
    return "\n".join(output)


def _protect_code(text: str) -> tuple[str, dict[str, str]]:
    """Protect code spans while surrounding Markdown is rewritten."""

    protected: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        key = f"\x00CODE{len(protected)}\x00"
        value = match.group(0)
        protected[key] = value if value.startswith("```") else f"```{value[1:-1]}```"
        return key

    return re.sub(r"```.*?```|`[^`\n]+`", replace, text, flags=re.DOTALL), protected


def format_whatsapp_text(text: str) -> str:
    """Convert common Markdown into formatting supported by WhatsApp."""

    if not text:
        return ""

    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = _convert_tables(value)
    value, protected_code = _protect_code(value)

    lines: list[str] = []
    in_fence = False
    for line in value.splitlines():
        if _FENCE.fullmatch(line):
            in_fence = not in_fence
            lines.append("```")
            continue
        if in_fence:
            lines.append(line)
            continue
        header = _HEADER.fullmatch(line)
        if header:
            lines.append(f"**{header.group(1)}**")
        elif _HORIZONTAL_RULE.fullmatch(line):
            lines.append("")
        else:
            lines.append(line)
    value = "\n".join(lines)

    value = _IMAGE.sub(lambda match: match.group(1), value)
    value = _LINK.sub(lambda match: f"{match.group(1)} ({match.group(2)})", value)
    value = _HTML_TAG.sub("", value)
    value = _MARKDOWN_QUOTE.sub("", value)
    value = _UNORDERED_LIST.sub(r"\1• ", value)
    value = _ORDERED_LIST.sub(r"\1• ", value)
    value = _STRIKETHROUGH.sub(r"~\1~", value)

    protected_bold: dict[str, str] = {}

    def protect_bold(match: re.Match[str]) -> str:
        key = f"\x00BOLD{len(protected_bold)}\x00"
        protected_bold[key] = f"*{match.group(1) or match.group(2)}*"
        return key

    value = _BOLD.sub(protect_bold, value)
    value = _ITALIC_ASTERISK.sub(r"_\1_", value)
    for key, bold in protected_bold.items():
        value = value.replace(key, bold)
    for key, code in protected_code.items():
        value = value.replace(key, code)

    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def format_for_whatsapp(text: str) -> str:
    """Alias for callers that describe formatting by destination."""

    return format_whatsapp_text(text)


def _chunk_at_boundary(remaining: str, max_length: int) -> tuple[str, str]:
    window = remaining[: max_length + 1]
    minimum_preferred = max_length // 2

    boundary = window.rfind("\n\n", 0, max_length + 1)
    separator_length = 2
    if boundary < minimum_preferred:
        boundary = window.rfind("\n", 0, max_length + 1)
        separator_length = 1
    if boundary < minimum_preferred:
        boundary = window.rfind(" ", 0, max_length + 1)
        separator_length = 1
    if boundary <= 0:
        boundary = max_length
        separator_length = 0

    chunk = remaining[:boundary].rstrip()
    rest = remaining[boundary + separator_length :].lstrip()
    if not chunk:  # Preserve progress for unusual leading whitespace.
        chunk = remaining[:max_length]
        rest = remaining[max_length:]
    return chunk, rest


def chunk_whatsapp_text(text: str, max_length: int = DEFAULT_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text without exceeding Meta's limit, preferring natural boundaries."""

    if not 1 <= max_length <= META_MAX_MESSAGE_LENGTH:
        raise ValueError(f"max_length must be between 1 and {META_MAX_MESSAGE_LENGTH}")

    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        chunk, remaining = _chunk_at_boundary(remaining, max_length)
        chunks.append(chunk)
    return chunks


class WhatsAppClient:
    """Small async client for the Meta WhatsApp Cloud API."""

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        *,
        version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
        max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH,
    ) -> None:
        if not access_token:
            raise ValueError("access_token is required")
        if not phone_number_id:
            raise ValueError("phone_number_id is required")
        if not re.fullmatch(r"v\d+(?:\.\d+)?", version):
            raise ValueError("version must look like 'v23.0'")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 1 <= max_message_length <= META_MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"max_message_length must be between 1 and {META_MAX_MESSAGE_LENGTH}"
            )

        self._max_message_length: int = max_message_length
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=f"{META_GRAPH_URL}/{version}/{phone_number_id}/messages",
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={
                "Authorization": f"Bearer {access_token}",
                "content-type": "application/json",
            },
        )

    @staticmethod
    def _error_for_response(response: httpx.Response) -> ProviderError:
        code: int | str | None = None
        error_type: str | None = None
        try:
            response_data = cast(object, response.json())
            if isinstance(response_data, Mapping):
                raw_error = cast(Mapping[object, object], response_data).get("error")
                if isinstance(raw_error, Mapping):
                    error = cast(Mapping[object, object], raw_error)
                    raw_code = error.get("code")
                    raw_type = error.get("type")
                    if isinstance(raw_code, (int, str)):
                        code = raw_code
                    if isinstance(raw_type, str):
                        error_type = raw_type
        except ValueError:
            pass

        metadata = ""
        if code is not None:
            metadata += f", code {code}"
        if error_type:
            metadata += f", type {error_type[:80]}"
        message = f"Meta API returned HTTP {response.status_code}{metadata}"
        if response.status_code in (408, 429) or response.status_code >= 500:
            return ProviderUnavailableError(_PROVIDER, message)
        return ProviderError(_PROVIDER, message)

    async def _post(
        self, payload: Mapping[str, JSONValue], *, expect_message_id: bool
    ) -> str | None:
        # one breaker for the Graph API. `_error_for_response` already separates a
        # 4xx we caused (ProviderError, permanent) from an outage or throttle
        # (ProviderUnavailableError, transient), so the default predicate counts
        # exactly the right ones
        with guard(_PROVIDER):
            try:
                response = await self._client.post("", json=dict(payload))
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailableError(_PROVIDER, "Meta API is unavailable") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(_PROVIDER, "Meta API request failed") from exc

            if response.status_code >= 400:
                raise self._error_for_response(response)

        try:
            data = cast(object, response.json())
        except ValueError as exc:
            raise ProviderError(_PROVIDER, "Meta API returned invalid JSON") from exc

        if not expect_message_id:
            return None
        messages: object = None
        if isinstance(data, Mapping):
            messages = cast(Mapping[object, object], data).get("messages")
        message: object = (
            cast(list[object], messages)[0] if isinstance(messages, list) and messages else None
        )
        message_id: object = None
        if isinstance(message, Mapping):
            message_id = cast(Mapping[object, object], message).get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ProviderError(_PROVIDER, "Meta API response did not include a message ID")
        return message_id

    async def send_text(
        self,
        to: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> list[str]:
        """Format, chunk, and send text; return one provider ID per chunk."""

        if not to:
            raise ValueError("to is required")
        formatted = format_whatsapp_text(text)
        if not formatted:
            raise ValueError("text must not be empty")

        message_ids: list[str] = []
        for index, chunk in enumerate(chunk_whatsapp_text(formatted, self._max_message_length)):
            payload: dict[str, JSONValue] = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            }
            if reply_to_message_id and index == 0:
                payload["context"] = {"message_id": reply_to_message_id}
            message_id = await self._post(payload, expect_message_id=True)
            if message_id is not None:
                message_ids.append(message_id)
        return message_ids

    async def mark_read(self, message_id: str) -> None:
        """Mark an inbound WhatsApp message as read."""

        if not message_id:
            raise ValueError("message_id is required")
        _ = await self._post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            },
            expect_message_id=False,
        )

    async def send_template(
        self,
        to: str,
        name: str,
        language: str = "en_US",
        components: Sequence[Mapping[str, JSONValue]] | None = None,
    ) -> str:
        """Send an approved template and return its provider message ID."""

        if not to:
            raise ValueError("to is required")
        if not name:
            raise ValueError("name is required")
        if not language:
            raise ValueError("language is required")

        template: dict[str, JSONValue] = {
            "name": name,
            "language": {"code": language},
        }
        if components is not None:
            template["components"] = [dict(component) for component in components]
        payload: dict[str, JSONValue] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": template,
        }
        message_id = await self._post(payload, expect_message_id=True)
        if message_id is None:  # Kept explicit for static type narrowing.
            raise ProviderError(_PROVIDER, "Meta API response did not include a message ID")
        return message_id

    async def send_document(
        self,
        to: str,
        link: str,
        filename: str = "",
        caption: str = "",
    ) -> str:
        """Send a document by URL and return its provider message ID.

        Meta fetches the link itself rather than accepting an upload here, so
        the caller passes a signed URL that outlives the fetch. Documents obey
        the same window rules as any other free-form message: this is for a
        conversation the owner already opened, not a cold ping.
        """

        if not to:
            raise ValueError("to is required")
        if not link:
            raise ValueError("link is required")

        document: dict[str, JSONValue] = {"link": link}
        if filename:
            document["filename"] = filename
        if caption:
            document["caption"] = caption[:1024]
        payload: dict[str, JSONValue] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "document",
            "document": document,
        }
        message_id = await self._post(payload, expect_message_id=True)
        if message_id is None:  # Kept explicit for static type narrowing.
            raise ProviderError(_PROVIDER, "Meta API response did not include a message ID")
        return message_id

    async def close(self) -> None:
        await self._client.aclose()


def meta_language(code: str) -> str:
    """Our language code, in Meta's dialect.

    Meta wants a locale (``en_US``, ``hi_IN``); the platform stores a language
    (``en``, ``hi``) because that is what the resolver produces and what reports
    group by. The mapping is small and explicit rather than clever: an unknown
    code passes through, and Meta rejects it loudly at the template, which is a
    better failure than a silently wrong locale.

    It lives on the adapter rather than in a sender because both the send path
    and the template *management* path need the same dialect, and two copies
    would drift the day someone adds a language to one of them.
    """

    table = {"en": "en_US", "hi": "hi_IN", "hinglish": "en_US", "bn": "bn_IN"}
    return table.get(code, code)


class WhatsAppTemplateClient:
    """Meta's template *management* API, which is a different endpoint entirely.

    Sending hangs off ``/{phone-number-id}/messages``; creating a template hangs
    off ``/{waba-id}/message_templates``, and the two are not interchangeable —
    which is why this is a second small client rather than a method on
    ``WhatsAppClient``, whose base URL is pinned to the messages endpoint.

    Submission is asynchronous on Meta's side: the POST returns an id and an
    initial status (almost always ``PENDING``), and the real verdict arrives
    later on the ``message_template_status_update`` webhook. So this client
    never claims a template is approved; it reports exactly what Meta said.
    """

    def __init__(
        self,
        access_token: str,
        waba_id: str,
        *,
        version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
    ) -> None:
        if not access_token:
            raise ValueError("access_token is required")
        if not waba_id:
            raise ValueError("waba_id is required")
        if not re.fullmatch(r"v\d+(?:\.\d+)?", version):
            raise ValueError("version must look like 'v23.0'")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=f"{META_GRAPH_URL}/{version}/{waba_id}/message_templates",
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={
                "Authorization": f"Bearer {access_token}",
                "content-type": "application/json",
            },
        )

    async def create(
        self, *, name: str, language: str, category: str, body: str
    ) -> tuple[str, str]:
        """Submit one template for review; return ``(template_id, status)``.

        ``status`` is Meta's own word (``PENDING``/``APPROVED``/...), passed up
        untranslated for the same reason migration 017 stores it that way: when
        a template is rejected the owner reads Meta's reason in Meta's words.
        """

        if not name:
            raise ValueError("name is required")
        if not language:
            raise ValueError("language is required")
        if not body:
            raise ValueError("body is required")

        payload: dict[str, JSONValue] = {
            "name": name,
            "language": language,
            "category": category.upper(),
            # One BODY component and nothing else. A header or buttons would
            # need their own review criteria and their own failure modes; the
            # campaign copy is a paragraph, so a paragraph is what is submitted.
            "components": [{"type": "BODY", "text": body}],
        }
        with guard(_PROVIDER):
            try:
                response = await self._client.post("", json=dict(payload))
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailableError(_PROVIDER, "Meta API is unavailable") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(_PROVIDER, "Meta API request failed") from exc

            if response.status_code >= 400:
                raise WhatsAppClient._error_for_response(response)

        try:
            data = cast(object, response.json())
        except ValueError as exc:
            raise ProviderError(_PROVIDER, "Meta API returned invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise ProviderError(_PROVIDER, "Meta API returned an unexpected template payload")
        fields = cast(Mapping[object, object], data)
        template_id = fields.get("id")
        status = fields.get("status")
        if not isinstance(template_id, (str, int)) or not str(template_id):
            raise ProviderError(_PROVIDER, "Meta API response did not include a template ID")
        return str(template_id), str(status) if isinstance(status, str) else "PENDING"

    async def close(self) -> None:
        await self._client.aclose()
