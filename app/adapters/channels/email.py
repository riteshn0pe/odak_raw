"""Outbound email rendering: Markdown answer to a text and HTML body pair.

Email is not chat. The answer the agent wrote is Markdown, and a mail client
shows Markdown as literal asterisks, so it has to be converted twice — once to
readable plain text and once to HTML.

The HTML converter is written here rather than pulled from a Markdown package,
for one reason: **the answer is untrusted output that a person will open in their
mail client.** Everything is HTML-escaped first and tags are then re-introduced
from a fixed allowlist, so a model that emits ``<script>`` or an
``onerror=`` attribute produces visible text and not markup. A general Markdown
library with raw-HTML passthrough is the opposite default.

Every message carries the loop-suppression header set. A mail loop with somebody
else's out-of-office is not a theoretical failure: it is the classic way an
autoresponder runs up a bill overnight.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.domain.email_text import (
    LOOP_HEADER,
    OUTBOUND_HTML_BOUNDARY,
    OUTBOUND_TEXT_MARKER,
    build_references,
    format_message_id,
    reply_subject,
)

# Only these schemes become clickable. A javascript: or data: href in a mail body
# is an attack, and a relative link cannot resolve outside a browser anyway.
_SAFE_SCHEMES = ("http://", "https://", "mailto:")

_FENCE = re.compile(r"^\s*```(?:[\w.+-]+)?\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_UNORDERED = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$")
_IMAGE = re.compile(r"!\[([^]]*)]\(([^)\s]*)(?:\s+[\"'][^\"']*[\"'])?\)")
_LINK = re.compile(r"\[([^]]+)]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?\S)\*\*|__(?=\S)(.+?\S)__")
_ITALIC = re.compile(r"(?<![*\w])\*(?=\S)([^*]+?\S)\*(?!\*)|(?<![_\w])_(?=\S)([^_]+?\S)_(?!_)")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_TABLE_PIPE = re.compile(r"^\s*\|.*\|\s*$")

_FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)
_BODY_STYLE = (
    f"margin:0;padding:0;font-family:{_FONT_STACK};font-size:15px;"
    "line-height:1.55;color:#1f2328;"
)
_CODE_STYLE = (
    "background:#f4f5f7;border-radius:4px;padding:1px 4px;"
    "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;"
)
_PRE_STYLE = (
    "background:#f4f5f7;border-radius:6px;padding:12px;overflow:auto;"
    "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;"
)
_QUOTE_STYLE = "margin:0 0 12px;padding:0 0 0 12px;border-left:3px solid #d0d7de;color:#57606a;"
_FOOTER_STYLE = "margin-top:24px;font-size:12px;color:#8b949e;"


@dataclass(frozen=True, slots=True)
class SourceLink:
    """One citation on an outbound email. ``url`` is optional and often absent."""

    label: str
    url: str | None = None


@dataclass(slots=True)
class RenderedEmail:
    """A message ready to hand to an EmailProvider."""

    subject: str
    text: str
    html: str
    message_id: str
    headers: dict[str, str] = field(default_factory=dict)


def _safe_href(url: str) -> str | None:
    candidate = (url or "").strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if not lowered.startswith(_SAFE_SCHEMES):
        return None
    if any(char in candidate for char in ("\n", "\r", '"', "'", "<", ">")):
        return None
    return html.escape(candidate, quote=True)


def _inline_html(fragment: str) -> str:
    """Inline Markdown to HTML. The fragment is escaped before any tag is added."""
    escaped = html.escape(fragment, quote=False)

    protected: dict[str, str] = {}

    def hold(markup: str) -> str:
        key = f"\x00H{len(protected)}\x00"
        protected[key] = markup
        return key

    escaped = _CODE_SPAN.sub(lambda m: hold(f'<code style="{_CODE_STYLE}">{m.group(1)}</code>'),
                             escaped)

    def image(match: re.Match[str]) -> str:
        # Remote images are stripped, never rendered: they are the standard
        # open-tracking pixel and most clients block them anyway.
        return match.group(1) or ""

    escaped = _IMAGE.sub(image, escaped)

    def link(match: re.Match[str]) -> str:
        label = match.group(1)
        href = _safe_href(html.unescape(match.group(2)))
        if href is None:
            return label
        return hold(f'<a href="{href}" style="color:#0969da;">{label}</a>')

    escaped = _LINK.sub(link, escaped)
    escaped = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
    escaped = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)

    for key, markup in protected.items():
        escaped = escaped.replace(key, markup)
    return escaped


def _inline_text(fragment: str) -> str:
    """Inline Markdown to plain text: keep the words, keep the URL, drop the syntax."""
    value = _IMAGE.sub(lambda m: m.group(1) or "", fragment)
    value = _LINK.sub(
        lambda m: m.group(1) if _safe_href(m.group(2)) is None else f"{m.group(1)} ({m.group(2)})",
        value,
    )
    value = _BOLD.sub(lambda m: m.group(1) or m.group(2), value)
    value = _ITALIC.sub(lambda m: m.group(1) or m.group(2), value)
    value = _CODE_SPAN.sub(lambda m: m.group(1), value)
    return value


def markdown_to_html(markdown: str) -> str:
    """Block-level Markdown to a fixed set of HTML tags. No raw HTML survives."""
    if not markdown:
        return ""
    source = markdown.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_stack: list[str] = []
    quote_open = False
    in_fence = False
    fence: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p style=\"margin:0 0 12px;\">{'<br />'.join(paragraph)}</p>")
            paragraph.clear()

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    def close_quote() -> None:
        nonlocal quote_open
        if quote_open:
            out.append("</blockquote>")
            quote_open = False

    for line in source.split("\n"):
        if _FENCE.fullmatch(line):
            if in_fence:
                body = html.escape("\n".join(fence), quote=False)
                out.append(f'<pre style="{_PRE_STYLE}"><code>{body}</code></pre>')
                fence.clear()
                in_fence = False
            else:
                flush_paragraph()
                close_lists()
                close_quote()
                in_fence = True
            continue
        if in_fence:
            fence.append(line)
            continue

        if not line.strip():
            flush_paragraph()
            close_lists()
            close_quote()
            continue

        if _RULE.fullmatch(line):
            flush_paragraph()
            close_lists()
            close_quote()
            out.append('<hr style="border:0;border-top:1px solid #d0d7de;margin:16px 0;" />')
            continue

        heading = _HEADING.fullmatch(line)
        if heading:
            flush_paragraph()
            close_lists()
            close_quote()
            level = min(len(heading.group(1)) + 1, 6)
            out.append(
                f'<h{level} style="margin:20px 0 8px;font-size:16px;">'
                f"{_inline_html(heading.group(2))}</h{level}>"
            )
            continue

        quote = _QUOTE.fullmatch(line)
        if quote:
            flush_paragraph()
            close_lists()
            if not quote_open:
                out.append(f'<blockquote style="{_QUOTE_STYLE}">')
                quote_open = True
            out.append(f'<p style="margin:0 0 8px;">{_inline_html(quote.group(1))}</p>')
            continue
        close_quote()

        unordered = _UNORDERED.fullmatch(line)
        ordered = _ORDERED.fullmatch(line)
        if unordered or ordered:
            flush_paragraph()
            wanted = "ul" if unordered else "ol"
            if not list_stack:
                out.append(f'<{wanted} style="margin:0 0 12px;padding-left:22px;">')
                list_stack.append(wanted)
            elif list_stack[-1] != wanted:
                out.append(f"</{list_stack.pop()}>")
                out.append(f'<{wanted} style="margin:0 0 12px;padding-left:22px;">')
                list_stack.append(wanted)
            content = unordered.group(2) if unordered else ordered.group(3)  # type: ignore[union-attr]
            out.append(f'<li style="margin:0 0 4px;">{_inline_html(content)}</li>')
            continue
        close_lists()

        if _TABLE_PIPE.match(line) and set(line.strip()) <= set("|-: "):
            # a Markdown table separator row carries no content
            continue
        paragraph.append(_inline_html(line.strip()))

    if in_fence and fence:
        body = html.escape("\n".join(fence), quote=False)
        out.append(f'<pre style="{_PRE_STYLE}"><code>{body}</code></pre>')
    flush_paragraph()
    close_lists()
    close_quote()
    return "\n".join(out)


def markdown_to_text(markdown: str) -> str:
    """Markdown to readable plain text. Structure survives as indentation."""
    if not markdown:
        return ""
    source = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    in_fence = False
    for line in source.split("\n"):
        if _FENCE.fullmatch(line):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(f"    {line}")
            continue
        heading = _HEADING.fullmatch(line)
        if heading:
            lines.append("")
            lines.append(_inline_text(heading.group(2)).upper())
            continue
        if _RULE.fullmatch(line):
            lines.append("")
            continue
        quote = _QUOTE.fullmatch(line)
        if quote:
            lines.append(f"> {_inline_text(quote.group(1))}")
            continue
        unordered = _UNORDERED.fullmatch(line)
        if unordered:
            lines.append(f"{unordered.group(1)}- {_inline_text(unordered.group(2))}")
            continue
        ordered = _ORDERED.fullmatch(line)
        if ordered:
            lines.append(f"{ordered.group(1)}{ordered.group(2)}. {_inline_text(ordered.group(3))}")
            continue
        lines.append(_inline_text(line))

    collapsed: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            collapsed.append(line.rstrip())
        else:
            blanks += 1
            if blanks <= 1:
                collapsed.append("")
    return "\n".join(collapsed).strip()


def loop_suppression_headers(loop_token: str) -> dict[str, str]:
    """The three headers that stop a conversation between two robots.

    ``Auto-Submitted: auto-replied`` is RFC 3834 and is what a well-behaved
    autoresponder on the other side checks before replying to us.
    ``X-Auto-Response-Suppress`` is the Exchange and Outlook equivalent.
    ``X-CA-Loop`` is ours, and it is the only one that also protects us against
    *ourselves* — our own inbound filter rejects anything carrying it, so a
    forwarding rule that points a channel back at itself dies on the first hop
    rather than on the invoice.
    """
    return {
        "Auto-Submitted": "auto-replied",
        "X-Auto-Response-Suppress": "All",
        LOOP_HEADER: loop_token,
    }


def _sources_block(sources: Sequence[SourceLink], *, link: bool) -> tuple[str, str]:
    """Footer listing the documents the answer came from, as text and HTML."""
    if not sources:
        return "", ""
    text_lines = ["", "Sources:"]
    html_items: list[str] = []
    for source in sources:
        href = _safe_href(source.url or "") if link else None
        label = source.label.strip() or "document"
        if href is None:
            text_lines.append(f"- {label}")
            html_items.append(f"<li>{html.escape(label, quote=False)}</li>")
        else:
            text_lines.append(f"- {label}: {source.url}")
            html_items.append(
                f'<li><a href="{href}" style="color:#0969da;">'
                f"{html.escape(label, quote=False)}</a></li>"
            )
    html_block = (
        f'<div style="{_FOOTER_STYLE}">Sources'
        f'<ul style="margin:4px 0 0;padding-left:20px;">{"".join(html_items)}</ul></div>'
    )
    return "\n".join(text_lines), html_block


def render_answer_email(
    *,
    answer: str,
    subject: str,
    thread_key: str,
    reply_seq: int,
    inbound_message_id: str,
    inbound_references: tuple[str, ...] = (),
    inbound_domain: str,
    loop_token: str,
    signature: str = "",
    sources: Sequence[SourceLink] = (),
    link_sources: bool = False,
    max_chars: int = 20_000,
) -> RenderedEmail:
    """Build the reply we send to a customer, both bodies and every header.

    ``Message-ID`` is generated here rather than left to the provider: inbound
    resolution parses our own id straight back to a thread, which is what keeps
    threading working when a client mangles the References header.

    The reply marker goes at the *end* of the body, above the quoted history, and
    is the anchor ``strip_quoted`` looks for first. Without it, threading depends
    entirely on heuristics against whatever mail client the customer uses.
    """
    body = (answer or "").strip()
    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars].rstrip()
        truncated = True

    text_source, html_sources = _sources_block(sources, link=link_sources)

    text_parts = [markdown_to_text(body)]
    if truncated:
        text_parts.append("[This reply was shortened. Reply to ask for the rest.]")
    if text_source:
        text_parts.append(text_source.lstrip("\n"))
    if signature.strip():
        text_parts.append(signature.strip())
    text_parts.append(OUTBOUND_TEXT_MARKER)
    text_body = "\n\n".join(part for part in text_parts if part)

    html_parts = [markdown_to_html(body)]
    if truncated:
        html_parts.append(
            f'<p style="{_FOOTER_STYLE}">This reply was shortened. '
            "Reply to ask for the rest.</p>"
        )
    if html_sources:
        html_parts.append(html_sources)
    if signature.strip():
        html_parts.append(
            f'<div style="{_FOOTER_STYLE}">'
            f'{markdown_to_html(signature.strip())}</div>'
        )
    html_parts.append(
        f'<p style="{_FOOTER_STYLE}">{html.escape(OUTBOUND_TEXT_MARKER, quote=False)}</p>'
    )
    # The boundary marks where quoted history begins for a client that keeps it.
    html_parts.append(OUTBOUND_HTML_BOUNDARY)
    html_body = (
        f'<div style="{_BODY_STYLE}">' + "\n".join(part for part in html_parts if part) + "</div>"
    )

    message_id = format_message_id(thread_key, reply_seq, inbound_domain)
    headers = loop_suppression_headers(loop_token)
    headers["Message-ID"] = message_id
    if inbound_message_id:
        headers["In-Reply-To"] = inbound_message_id
    references = build_references(inbound_references, inbound_message_id)
    if references:
        headers["References"] = references

    return RenderedEmail(
        subject=reply_subject(subject),
        text=text_body,
        html=html_body,
        message_id=message_id,
        headers=headers,
    )
