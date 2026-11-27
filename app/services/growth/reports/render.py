"""Server-side rendering for the report agent: SVG charts and the PDF.

One server-side charting approach, per the spec: charts are SVG strings built
in plain Python (no headless browser, no new dependency) and embedded into the
PDF template, which PyMuPDF assembles — the same library the ingestion
pipeline already depends on for parsing and OCR rasters.

Only sections with data render. A tenant with no activity gets a one-section
report (summary tiles at zero plus the settings snapshot), not an error.
"""

from __future__ import annotations

import html as _html
from typing import Any

import fitz  # type: ignore[import-untyped]  # no stubs; used like gate.py uses it

CHART_W = 520
BAR_H = 22
BAR_GAP = 10
FONT = "helv"

# A tenant with nothing in the window still gets these two sections: the
# summary says what happened (nothing, and that is information), and the
# snapshot says what is connected. Everything else renders only with data.
ALWAYS_SECTIONS: tuple[str, ...] = ("summary", "settings")


def _esc(value: Any) -> str:
    return _html.escape(str(value), quote=True)


def bar_svg(title: str, items: list[tuple[str, int]], color: str = "#2563eb") -> str:
    """Horizontal bars, one row per item. Empty input is an empty chart."""
    rows = [(label, max(0, int(value))) for label, value in items if int(value) > 0]
    peak = max([value for _, value in rows] + [1])
    height = 30 + len(rows) * (BAR_H + BAR_GAP)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CHART_W}" height="{height}">',
        f'<text x="0" y="18" font-size="14" font-family="sans-serif">{_esc(title)}</text>',
    ]
    y = 30
    for label, value in rows:
        width = max(2, int((CHART_W - 220) * value / peak))
        parts.append(
            f'<text x="0" y="{y + 15}" font-size="11" font-family="sans-serif">'
            f"{_esc(label[:28])}</text>"
            f'<rect x="150" y="{y}" width="{width}" height="{BAR_H}" fill="{color}" rx="3"/>'
            f'<text x="{152 + width}" y="{y + 15}" font-size="11" '
            f'font-family="sans-serif">{value}</text>'
        )
        y += BAR_H + BAR_GAP
    parts.append("</svg>")
    return "".join(parts)


def line_svg(
    title: str, points: list[tuple[str, float]], color: str = "#16a34a"
) -> str:
    """One polyline over labelled points. Fewer than two points: axes only."""
    height = 200
    pad_left, pad_bottom, pad_top = 44, 30, 30
    plot_w = CHART_W - pad_left - 10
    plot_h = height - pad_top - pad_bottom
    peak = max([value for _, value in points] + [1.0])
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CHART_W}" height="{height}">',
        f'<text x="0" y="18" font-size="14" font-family="sans-serif">{_esc(title)}</text>',
        f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" '
        f'y2="{pad_top + plot_h}" stroke="#999"/>',
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{pad_left + plot_w}" '
        f'y2="{pad_top + plot_h}" stroke="#999"/>',
    ]
    if len(points) >= 2:
        step = plot_w / (len(points) - 1)
        coords = [
            (
                pad_left + index * step,
                pad_top + plot_h - (value / peak) * plot_h,
            )
            for index, (_, value) in enumerate(points)
        ]
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords)
        )
        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>'
        )
        for (x, y), (label, value) in zip(coords, points, strict=True):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
            parts.append(
                f'<text x="{x:.1f}" y="{pad_top + plot_h + 14}" font-size="9" '
                f'text-anchor="middle" font-family="sans-serif">{_esc(label)}</text>'
            )
            _ = value
    parts.append("</svg>")
    return "".join(parts)


def has_activity(numbers: dict[str, Any]) -> bool:
    """Whether the window holds anything beyond the always-on sections."""
    funnel = numbers.get("funnel") or {}
    if int(funnel.get("leads_created") or 0):
        return True
    if int(funnel.get("inbound") or 0):
        return True
    if any(int(item.get("count") or 0) for item in funnel.get("outbound") or []):
        return True
    return bool(numbers.get("hot_leads"))


def fallback_summary(numbers: dict[str, Any]) -> str:
    """The deterministic two-line summary, used with no data or no LLM.

    Two lines, plain words, every number straight from the query result — so
    this is also what a block-mode grounding refusal degrades to: information
    without any invented claim.
    """
    funnel = numbers.get("funnel") or {}
    leads = int(funnel.get("leads_created") or 0)
    mailed = sum(
        int(item.get("count") or 0)
        for item in funnel.get("outbound") or []
        if item.get("status") in ("sent", "delivered", "opened")
    )
    replies = int(funnel.get("inbound") or 0)
    rate = f"{replies / mailed:.0%}" if mailed else "—"
    period = str(numbers.get("period") or "period")
    hot = numbers.get("hot_leads") or []
    second = (
        "Nothing to act on — no hot leads waiting."
        if not hot
        else f"{len(hot)} hot lead(s) waiting on you — see the report."
    )
    return (
        f"{period.capitalize()} report: {leads} new leads, {mailed} mails sent, "
        f"{replies} replies ({rate} reply rate).\n{second}"
    )


def whatsapp_text(summary: str, pdf_url: str, panel_url: str) -> str:
    """Plain text for the owner's WhatsApp: two lines plus the two links."""
    lines = [summary.strip()]
    if pdf_url:
        lines.append(f"Full report (PDF): {pdf_url}")
    if panel_url:
        lines.append(f"Details: {panel_url}")
    return "\n".join(lines)


def whatsapp_template_param(summary: str) -> str:
    """The summary as one WhatsApp template body parameter.

    Meta rejects a template parameter containing a newline or a tab (error
    132000), so the two summary lines are joined with a separator instead of
    a line break. The links do not travel in the parameter at all — the PDF
    goes as a document message and the panel link as its caption.
    """
    flat = " ".join(summary.split())
    return flat[:1024]


def telegram_report_payload(
    summary: str, pdf_url: str, panel_url: str
) -> dict[str, Any]:
    """sendMessage body for one report, HTML-escaped with URL buttons."""
    text = _html.escape(summary.strip())
    if pdf_url:
        text += f'\n\n<a href="{_esc(pdf_url)}">Full report (PDF)</a>'
    buttons: list[list[dict[str, str]]] = []
    if panel_url:
        buttons = [[{"text": "📊 Open reports", "url": panel_url}]]
    payload: dict[str, Any] = {
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return payload


# ------------------------------------------------------------------- the PDF


def build_pdf(
    numbers: dict[str, Any],
    summary: str,
    tenant_label: str,
    panel_base: str,
) -> bytes:
    """Assemble the branded PDF. Only sections with data render.

    Charts are the SVG builders above, converted through PyMuPDF's SVG input
    and placed as vector pages — no rasterisation, no browser. Links point at
    the tenant's panel pages and, for hot leads, at the lead record.
    """
    doc = fitz.open()
    toc: list[tuple[int, str, int]] = []
    sections = _plan_sections(numbers)
    period = str(numbers.get("period") or "")

    for key, title in sections:
        # The page this section opens on, as it will be numbered once the
        # contents page is inserted at the front. A section may run onto
        # further pages; the contents entry points at where it starts.
        first_page = doc.page_count + 1
        toc.append((1, title, first_page))
        flow = _Flow(doc, tenant_label, period, title)
        if key == "summary":
            _section_summary(flow, numbers, summary)
        elif key == "funnel":
            _section_funnel(flow, numbers)
        elif key == "replies":
            _section_replies(flow, numbers)
        elif key == "money":
            _section_money(flow, numbers)
        elif key == "customers":
            _section_customers(flow, numbers)
        elif key == "insights":
            _section_insights(flow, numbers)
        elif key == "cost":
            _section_cost(flow, numbers)
        elif key == "settings":
            _section_settings(flow, numbers, panel_base)

    _contents_page(doc, toc, panel_base)
    # `set_toc` numbers pages from 1; the entries above are the 0-based index
    # the in-document links use, so the outline needs one more.
    doc.set_toc([[level, title, page + 1] for level, title, page in toc])
    out: bytes = doc.tobytes()
    doc.close()
    return out


def _plan_sections(numbers: dict[str, Any]) -> list[tuple[str, str]]:
    """Contents, in order. Summary and settings always; the rest with data."""
    sections = [("summary", "Summary")]
    funnel = numbers.get("funnel") or {}
    outbound = [i for i in funnel.get("outbound") or [] if int(i.get("count") or 0)]
    if int(funnel.get("leads_created") or 0) or outbound:
        sections.append(("funnel", "Leads & outreach"))
    if (
        int(funnel.get("inbound") or 0)
        or numbers.get("hot_leads")
        or numbers.get("trend")
    ):
        sections.append(("replies", "Replies & hot leads"))
    campaigns = numbers.get("campaigns") or {}
    if campaigns.get("by_status") or int(
        campaigns.get("store_revenue_micros") or 0
    ):
        sections.append(("money", "Campaigns & money"))
    customers = numbers.get("customers") or {}
    if int(customers.get("total") or 0):
        sections.append(("customers", "Customers"))
    insights = numbers.get("insights") or {}
    if int(insights.get("total") or 0) or (numbers.get("events") or {}).get(
        "upcoming_30d"
    ):
        sections.append(("insights", "Insights & events"))
    if (numbers.get("usage") or {}).get("total_micros"):
        sections.append(("cost", "Usage & cost"))
    sections.append(("settings", "Settings snapshot"))
    return sections


def _header(page: Any, tenant_label: str, period: str, title: str) -> None:
    page.insert_text((40, 40), tenant_label[:60] or "Growth report", fontsize=11)
    page.insert_text((40, 60), f"{period} report — {title}", fontsize=18)
    page.draw_line((40, 74), (555, 74))


PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0
BODY_TOP = 110.0
BODY_BOTTOM = 800.0


class _Flow:
    """A writing cursor that spills onto a new page instead of off the bottom.

    The sections do not know how much room they need — a tenant with three
    charts and ten hot leads overruns one page and a quiet tenant fills a
    third of it. Truncating the overflow silently would put a number in the
    report that nobody can see, so the cursor adds a page and carries on.
    """

    def __init__(self, doc: Any, tenant_label: str, period: str, title: str) -> None:
        self._doc = doc
        self._label = tenant_label
        self._period = period
        self._title = title
        self.page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        _header(self.page, tenant_label, period, title)
        self.y = BODY_TOP

    def _spill(self) -> None:
        self.page = self._doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        _header(self.page, self._label, self._period, f"{self._title} (cont.)")
        self.y = BODY_TOP

    def reserve(self, height: float) -> None:
        """Guarantee ``height`` points of room below the cursor."""
        if self.y + height > BODY_BOTTOM and self.y > BODY_TOP:
            self._spill()

    def text(self, lines: list[str]) -> None:
        for line in lines:
            for chunk in _wrap(line, 95):
                self.reserve(14)
                self.page.insert_text((40, self.y), chunk, fontsize=10)
                self.y += 14
        self.y += 6

    def svg(self, svg: str, height: float = 240) -> None:
        self.reserve(height + 10)
        _place_svg(self.page, svg, self.y, height)
        self.y += height + 10


def _wrap(line: str, width: int) -> list[str]:
    words, out, current = line.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out or [""]


def _place_svg(page: Any, svg: str, top: float, height: float = 240) -> None:
    """Embed one SVG chart as vectors. Never raises: a chart that will not
    convert is a missing picture, not a missing report."""
    try:
        svg_doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
        chart = fitz.open(stream=svg_doc.convert_to_pdf(), filetype="pdf")
        page.show_pdf_page(fitz.Rect(40, top, 560, top + height), chart, 0)
        chart.close()
        svg_doc.close()
    except Exception:
        page.insert_text((40, top + 20), "(chart unavailable)", fontsize=10)


def _section_summary(
    flow: _Flow, numbers: dict[str, Any], summary: str
) -> None:
    funnel = numbers.get("funnel") or {}
    # Same definition as `fallback_summary`: a mail that is still queued or
    # that bounced was not "mailed", and the tile must not disagree with the
    # two-line summary printed under it.
    mailed = sum(
        int(item.get("count") or 0)
        for item in funnel.get("outbound") or []
        if item.get("status") in ("sent", "delivered", "opened")
    )
    deltas = numbers.get("deltas") or {}
    tiles = [
        ("New leads", int(funnel.get("leads_created") or 0), deltas.get("leads")),
        ("Mailed", mailed, deltas.get("mailed")),
        ("Replies", int(funnel.get("inbound") or 0), deltas.get("replies")),
    ]
    x = 40.0
    top = flow.y
    for label, value, delta in tiles:
        flow.page.draw_rect((x, top, x + 160, top + 56))
        flow.page.insert_text((x + 10, top + 20), label, fontsize=10)
        flow.page.insert_text((x + 10, top + 42), str(value), fontsize=18)
        if delta is not None:
            flow.page.insert_text(
                (x + 90, top + 42), _delta_text(delta), fontsize=10
            )
        x += 172
    flow.y = top + 70
    lines = [line.strip() for line in summary.strip().splitlines() if line.strip()]
    flow.text(lines[:8])


def _delta_text(delta: Any) -> str:
    try:
        value = int(delta)
    except (TypeError, ValueError):
        return ""
    return f"{'+' if value >= 0 else ''}{value} vs last"


def _section_funnel(flow: _Flow, numbers: dict[str, Any]) -> None:
    funnel = numbers.get("funnel") or {}
    items = [
        (str(item.get("source") or "?"), int(item.get("count") or 0))
        for item in funnel.get("by_source") or []
    ]
    if items:
        flow.svg(bar_svg("Leads by source", items))
    mailed = sum(
        int(i.get("count") or 0)
        for i in funnel.get("outbound") or []
        if i.get("status") in ("sent", "delivered", "opened")
    )
    delivered = sum(
        int(i.get("count") or 0)
        for i in funnel.get("outbound") or []
        if i.get("status") in ("delivered", "opened")
    )
    snapshot = {
        i.get("status"): int(i.get("count") or 0)
        for i in funnel.get("status_snapshot") or []
    }
    stages = [
        ("Mailed", mailed),
        ("Delivered", delivered),
        ("Replied", int(funnel.get("inbound") or 0)),
        ("Positive", int(snapshot.get("positive") or 0)),
        ("Meeting", int(snapshot.get("meeting") or 0)),
    ]
    flow.svg(bar_svg("Funnel", stages, color="#7c3aed"))
    if numbers.get("trends_6m"):
        points = [
            (bucket["month"][2:], float(bucket["replies"]))
            for bucket in numbers["trends_6m"]
        ]
        flow.svg(line_svg("Replies per month (6m)", points), 220)


def _section_replies(flow: _Flow, numbers: dict[str, Any]) -> None:
    trend = numbers.get("trend") or []
    if trend:
        points = [
            (bucket["day"][5:], float(bucket["reply_rate"])) for bucket in trend
        ]
        flow.svg(line_svg("Reply rate by day", points), 220)
    reply_time = numbers.get("reply_time") or {}
    median = reply_time.get("median_seconds")
    median_text = (
        f"Median time to first reply: {median / 3600:.1f}h "
        f"over {reply_time.get('count', 0)} threads."
        if median is not None
        else "No first replies in this window."
    )
    flow.text([median_text])
    hot = numbers.get("hot_leads") or []
    if hot:
        flow.text(["Hot leads waiting:"])
        # each lead is one line, so a spill never separates a lead from its record
        for lead in hot[:10]:
            flow.text(
                [
                    f"• {lead.get('company') or lead.get('email') or '?'} "
                    f"({lead.get('status')}) — "
                    f"{lead.get('person') or 'unknown contact'}"
                ]
            )
    by_lang = numbers.get("by_language") or []
    if by_lang:
        flow.text(
            [
                "Reply rate by language: "
                + ", ".join(
                    f"{r['language']} {r['reply_rate']:.0%}" for r in by_lang[:6]
                )
            ]
        )


def _link_row(page: Any, y: float, label: str) -> Any:
    page.insert_text((40, y), label, fontsize=9)
    return fitz.Rect(40, y - 10, 200, y + 2)


def _section_money(flow: _Flow, numbers: dict[str, Any]) -> None:
    campaigns = numbers.get("campaigns") or {}
    lines = [
        f"{item['status']}: {item['count']}"
        for item in campaigns.get("by_status") or []
    ]
    # Two labels, because they are two claims: what the shop took, and what a
    # campaign can prove it caused. Conflating them flatters the campaigns.
    store_revenue = int(campaigns.get("store_revenue_micros") or 0)
    attributed = int(campaigns.get("attributed_revenue_micros") or 0)
    if store_revenue:
        lines.append(f"Store revenue in this window: {_rupees(store_revenue)}")
    if attributed:
        lines.append(f"Attributed to campaigns (discount code): {_rupees(attributed)}")
    flow.text(lines or ["No campaign activity."])


def _section_customers(flow: _Flow, numbers: dict[str, Any]) -> None:
    customers = numbers.get("customers") or {}
    lines = [
        f"Customers: {int(customers.get('total') or 0)} "
        f"({int(customers.get('new') or 0)} new)"
    ]
    for payment, row in (customers.get("orders") or {}).items():
        lines.append(
            f"Orders ({payment}): {row['count']} — {_rupees(row['revenue_micros'])}"
        )
    flow.text(lines)


def _section_insights(flow: _Flow, numbers: dict[str, Any]) -> None:
    insights = numbers.get("insights") or {}
    lines = [
        f"Insights: {int(insights.get('total') or 0)} total, "
        f"{int(insights.get('acted_on') or 0)} acted on."
    ]
    for item in insights.get("by_type") or []:
        lines.append(f"• {item['type']}: {item['count']}")
    for event in (numbers.get("events") or {}).get("upcoming_30d") or []:
        lines.append(f"• {event['date']}: {event['name']} ({event['region']})")
    flow.text(lines)


def _section_cost(flow: _Flow, numbers: dict[str, Any]) -> None:
    usage = numbers.get("usage") or {}
    lines = [f"Total metered cost: {_rupees(int(usage.get('total_micros') or 0))}"]
    for row in usage.get("by_feature") or []:
        lines.append(f"• {row['feature']}: {_rupees(int(row['micros']))}")
    per_lead = usage.get("cost_per_lead_micros")
    per_reply = usage.get("cost_per_reply_micros")
    if per_lead is not None:
        lines.append(f"Cost per lead: {_rupees(int(per_lead))}")
    if per_reply is not None:
        lines.append(f"Cost per reply: {_rupees(int(per_reply))}")
    flow.text(lines)


def _section_settings(
    flow: _Flow, numbers: dict[str, Any], panel_base: str
) -> None:
    snapshot = numbers.get("settings_snapshot") or {}
    lines = ["Connected:"]
    for row in snapshot.get("connected") or []:
        lines.append(f"• {row['provider']}: {row['status']}")
    if not (snapshot.get("connected") or []):
        lines.append("• nothing connected yet")
    if snapshot.get("broken"):
        lines.append("Needs attention:")
        for row in snapshot["broken"]:
            lines.append(f"• {row['provider']}: {row['status']}")
    flow.text(lines)
    if panel_base:
        flow.reserve(20)
        url = f"{panel_base}/growth/integrations"
        rect = _link_row(flow.page, flow.y, "Open connections →")
        flow.page.insert_link({"kind": 2, "from": rect, "uri": url})
        flow.y += 20


def _contents_page(doc: Any, toc: list[tuple[int, str, int]], panel_base: str) -> None:
    """First page: clickable contents; every chart links to its panel page."""
    page = doc.new_page(pno=0, width=595, height=842)
    page.insert_text((40, 40), "Contents", fontsize=18)
    y = 80.0
    # only the panels that exist in the dashboard. anything else would draw a
    # link that lands on a 404 or the SPA's fallback screen
    panel_paths = {
        "Summary": "/growth/reports",
        "Settings snapshot": "/growth/integrations",
    }
    for _, title, pageno in toc:
        page.insert_text((40, y), f"{title}  …  p.{pageno + 1}", fontsize=11)
        page.insert_link(
            {"kind": 1, "from": fitz.Rect(40, y - 11, 400, y + 3), "page": pageno}
        )
        if panel_base and title in panel_paths:
            page.insert_link(
                {
                    "kind": 2,
                    "from": fitz.Rect(410, y - 11, 520, y + 3),
                    "uri": f"{panel_base}{panel_paths[title]}",
                }
            )
            page.insert_text((410, y), "open panel →", fontsize=9)
        y += 22


def _rupees(micros: int) -> str:
    """Micros (millionths of a major unit) as money, in the PDF's own font.

    Spelled "INR" rather than ₹ on purpose: the built-in Helvetica the pages
    use has no rupee glyph, so the symbol renders as a stray dot — a money
    figure nobody can read is worse than three plain letters.
    """
    return f"INR {micros / 1_000_000:,.2f}"
