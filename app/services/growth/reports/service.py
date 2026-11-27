"""The report agent: weekly Monday 9am, monthly 1st 9am, on demand.

Day-one value: the owner wakes up to what happened, in two lines on chat and a
branded PDF in the panel. All numbers come from ``queries.py`` (plain SQL, no
LLM); the model writes only the word summary, and the grounding helper checks
that it invented no number.

No HITL — a report is information, not a money action. Level 0 semantics
already.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_report"
FEATURE = "report"
REPORT_JOB = "growth_report"
DUE_TASK = "growth_report_due"
MAX_TENANTS_PER_TICK = 200

# The per-tenant runtime budget. The queries are milliseconds of COUNTs; the
# only unbounded leg is the summary LLM call, which gets its own shorter
# leash inside `run`. A tenant that blows this still holds a `queued` row the
# next tick regenerates, so slowness delays a report rather than losing one.
RUN_TIMEOUT_SECONDS = 55.0
# Delivery gets its own budget after the build, not a share of it: an upload
# to Telegram or Meta that runs long must not mark a finished report failed.
DELIVER_TIMEOUT_SECONDS = 45.0
SUMMARY_TIMEOUT_SECONDS = 25.0
# Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
SUMMARY_MAX_TOKENS = 20000

# How long a failed or stuck report waits before the hourly tick tries it
# again. Four hours gives a transient provider outage time to end and still
# lands the report the same morning.
RETRY_BACKOFF = timedelta(hours=4)

# WhatsApp template used for the owner's report ping when the tenant has an
# approved utility template of this name. Otherwise the ping goes free-form
# with the PDF link — same posture as the approval notifier.
WA_TEMPLATE_NAME = "owner_report"

_SYSTEM = (
    "You write a small business owner's report summary: exactly five lines — "
    "the two-line summary, then WENT WELL:, DROPPED: and DO THIS: lines. "
    "Plain words, no jargon, no exclamation marks, no motivational language. "
    "Every number you write must appear in the NUMBERS — inventing one is "
    "the worst thing you can do. \"nothing dropped\" is a valid and expected "
    "answer for DROPPED."
)

_PROMPT = """NUMBERS (this period's computed metrics as JSON):
{numbers}

Write exactly five lines:
line 1-2: the two-line summary (leads, mails, replies, reply rate)
line 3: WENT WELL: <one specific thing, with its number>
line 4: DROPPED: <one specific thing, with its number, or "nothing dropped">
line 5: DO THIS: <the single most useful next action>

If the NUMBERS are followed by AGENT NOTES, those are what the other agents
found today; you may draw on them, and any number you take from a note must
appear in it.
"""

# Cap on the agent-notes block that rides in the summary prompt and evidence.
# The numbers already take most of that budget, and day-old detail beyond the
# cap changes no decision a two-line summary makes.
FINDINGS_MAX_CHARS = 3000


def _findings_block(findings: dict[str, dict[str, list[dict[str, Any]]]]) -> str:
    """Render the day's run context as the AGENT NOTES block.

    One line per entry, agent and kind labelled, payload as JSON, capped.
    Deterministic so the same day renders the same block in the prompt and in
    the grounding evidence — the judge must see exactly what the writer saw.
    """
    import json

    lines: list[str] = []
    for agent_key in sorted(findings):
        for kind in sorted(findings[agent_key]):
            for entry in findings[agent_key][kind]:
                payload = json.dumps(entry.get("payload") or {}, default=str)
                lines.append(f"{agent_key} {kind}: {payload}")
    return "\n".join(lines)[:FINDINGS_MAX_CHARS]


class ReportService:
    """Computes, renders, stores and delivers one tenant's report."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID) -> str:
        """The persona, rendered from this tenant's facts at invocation.

        Only the persona is rendered: the five-line shape the grounding judge
        reads is instructed by the user prompt and checked against the NUMBERS
        evidence, both untouched here. Falls back to the module constant when
        the agent layer is absent or a required fact is missing — the card
        names the gap; a report must not be lost to one.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _SYSTEM
        try:
            return await prompts.system_for(tenant_id, AGENT_KEY)
        except MissingFacts:
            logger.warning(
                "agent_prompt_missing_facts",
                agent_key=AGENT_KEY,
                tenant_id=str(tenant_id),
            )
            return _SYSTEM

    # ------------------------------------------------------------ cron fan-out

    async def run_due(self) -> dict[str, Any]:
        """One ``growth_report_due`` pass: enqueue a job per due tenant.

        Queued, never run inline — fifty tenants queue, none blocks the tick.
        Candidate selection mirrors the dispatch tick: admin-scope list, then
        per-tenant scope with one try/except each.
        """
        from app.adapters.postgres.growth_repo import active_tenant_ids

        tenants = await active_tenant_ids(limit=MAX_TENANTS_PER_TICK)
        enqueued = 0
        skipped = 0
        failures: list[str] = []
        for tenant_id in tenants:
            try:
                if not await self._c.growth.settings.has_settings(tenant_id):
                    skipped += 1
                    continue
                due = await self.due_for(tenant_id)
                for item in due:
                    await self._enqueue(tenant_id, item["period"], item["start"])
                    enqueued += 1
            except Exception:
                failures.append(str(tenant_id))
                logger.warning(
                    "growth_report_due_failed",
                    task=DUE_TASK,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
        return {
            "status": "ok",
            "tenants": len(tenants),
            "skipped": skipped,
            "enqueued": enqueued,
            "failed": failures,
        }

    async def due_for(
        self, tenant_id: UUID, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Which scheduled periods are due for this tenant right now.

        Pure-ish: reads settings and the reports ledger, writes nothing. Split
        out so the on-demand route and the unit tests can call it without
        enqueueing anything.
        """
        from app.services.growth.reports.windows import periods_due

        settings = await self._c.growth_profile.get(tenant_id)
        current = now or datetime.now(UTC)
        candidates = periods_due(settings, current)
        due: list[dict[str, Any]] = []
        for candidate in candidates:
            existing = await self._c.growth.reports.for_period(
                tenant_id, candidate["period"], candidate["start"]
            )
            if existing is not None:
                if existing.get("status") == "ready":
                    continue
                # A failed or still-queued row is retried, but not on every one
                # of the day's remaining ticks: each rebuild pays for a summary
                # LLM call and a metered WhatsApp conversation, so a tenant
                # whose report keeps failing must not be billed fifteen times
                # for it. The period stays due once the backoff elapses.
                touched = existing.get("updated_at") or existing.get("created_at")
                if isinstance(touched, datetime):
                    if touched.tzinfo is None:
                        touched = touched.replace(tzinfo=UTC)
                    if current - touched < RETRY_BACKOFF:
                        continue
            due.append(candidate)
        return due

    async def request_on_demand(self, tenant_id: UUID, *, via: str) -> dict[str, Any]:
        """The one on-demand path, shared by the panel and the Telegram bot.

        Dedupe lives here rather than at each caller because the two doors must
        agree: a `/report` message and a "Run now" press on the same day are
        the same request, and a second one must not pay for a second PDF, a
        second summary call and a second metered WhatsApp conversation.

        Raises ValidationError when on-demand reports are off for the tenant.
        """
        from app.core.errors import ValidationError

        settings = await self._c.growth_profile.get(tenant_id)
        if not (settings.get("report_prefs") or {}).get("on_demand", True):
            raise ValidationError("on-demand reports are off for this workspace")

        today = datetime.now(UTC).date()
        existing = await self._c.growth.reports.for_period(
            tenant_id, "on_demand", today
        )
        status = str((existing or {}).get("status") or "")
        if existing is not None and status in ("ready", "queued"):
            return {"status": status, "report_id": str(existing["id"])}

        await self._enqueue(tenant_id, "on_demand", today)
        await self._c.growth.audit.record(
            tenant_id,
            agent="owner",
            action="report_requested",
            input={"via": via},
            reason=f"on-demand report requested via {via}",
        )
        return {"status": "queued"}

    async def _enqueue(
        self, tenant_id: UUID, period: str, start: Any
    ) -> None:
        from app.adapters.queue.base import Job

        try:
            await self._c.queue.enqueue(
                Job(
                    kind=REPORT_JOB,
                    tenant_id=tenant_id,
                    payload={"period": period, "period_start": str(start)},
                )
            )
        except Exception:
            logger.warning(
                "growth_report_enqueue_failed",
                tenant_id=str(tenant_id),
                period=period,
                exc_info=True,
            )

    # ------------------------------------------------------------------- run

    async def run(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Build one report: numbers → summary → PDF → delivery."""
        from app.services.growth.reports.windows import resolve_window

        period = str(payload.get("period") or "on_demand")
        if period not in ("weekly", "monthly", "daily", "on_demand"):
            return {"status": "skipped", "reason": f"unknown period {period!r}"}

        settings = await self._c.growth_profile.get(tenant_id)
        if period == "on_demand" and not (settings.get("report_prefs") or {}).get(
            "on_demand", True
        ):
            return {"status": "skipped", "reason": "on-demand reports are off"}

        start, end = resolve_window(
            settings, period, str(payload.get("period_start") or "")
        )
        # the dedupe key: the window's first date for scheduled runs, today for
        # on-demand (whose window is "the last 7 days ending now" and would
        # otherwise drift a day on every press). The panel route checks this
        # same date, so both callers land on the same row.
        period_start = end.date() if period == "on_demand" else start.date()
        row = await self._c.growth.reports.create(
            tenant_id, period=period, period_start=period_start
        )
        if not row:
            return {"status": "skipped", "reason": "ledger write failed"}
        report_id = UUID(str(row["id"]))
        if row.get("status") == "ready" and period != "on_demand":
            # a redelivered job for a finished scheduled period: the PDF is in
            # storage and the chats already went out
            return {"status": "already", "report_id": str(report_id)}

        try:
            built = await asyncio.wait_for(
                self._build(tenant_id, report_id, settings, period, start, end),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.error(
                "growth_report_failed",
                tenant_id=str(tenant_id),
                report_id=str(report_id),
                exc_info=True,
            )
            await self._c.growth.reports.mark_failed(tenant_id, report_id)
            await self._c.growth.audit.record(
                tenant_id,
                agent=AGENT_KEY,
                action="report_failed",
                input={"period": period, "period_start": start.date().isoformat()},
                reason="see job log",
            )
            raise

        # Delivery sits outside the build budget on purpose. A Telegram upload
        # that runs long must not turn a report whose PDF is already in storage
        # into a `failed` row, and must not make QStash rebuild the whole thing.
        delivered: dict[str, int] = {}
        try:
            delivered = await asyncio.wait_for(
                self._deliver(
                    tenant_id,
                    report_id,
                    built["summary"],
                    built["pdf"],
                    built["pdf_path"],
                    built["panel_base"],
                ),
                timeout=DELIVER_TIMEOUT_SECONDS,
            )
        except Exception:
            # a report that exists but did not reach the owner's chat is still
            # a report: the panel row and the PDF stand, so this is a warning
            logger.warning(
                "growth_report_delivery_failed",
                tenant_id=str(tenant_id),
                report_id=str(report_id),
                exc_info=True,
            )
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="report_ready",
            input={"period": period, "period_start": start.date().isoformat()},
            output={"report_id": str(report_id), "delivered": delivered},
            reason="scheduled" if period != "on_demand" else "on demand",
        )
        return {
            "status": "ready",
            "report_id": str(report_id),
            "delivered": delivered,
        }

    async def _build(
        self,
        tenant_id: UUID,
        report_id: UUID,
        settings: dict[str, Any],
        period: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Numbers → summary → PDF → ledger. Delivery is the caller's job."""
        from app.services.growth.metering import meter
        from app.services.growth.reports import queries, render

        months_back = 6 if period == "monthly" else 0
        numbers = await queries.collect(
            self._c, tenant_id, period=period, start=start, end=end,
            months_back=months_back,
        )
        numbers["deltas"] = await self._deltas(numbers, tenant_id, period, start, end)
        findings = await self._day_findings(tenant_id, settings)

        summary = await self._summarise(tenant_id, numbers, findings)
        profile = settings.get("profile") or {}
        tenant_label = str(profile.get("sender_name") or profile.get("sells") or "")
        panel_base = self._panel_base()
        pdf = render.build_pdf(numbers, summary, tenant_label, panel_base)
        pdf_path = f"tenants/{tenant_id}/reports/{report_id}.pdf"
        await self._c.storage.put(pdf_path, pdf, "application/pdf")
        await self._c.growth.reports.mark_ready(
            tenant_id, report_id, numbers=numbers, pdf_path=pdf_path, summary=summary
        )
        await meter(tenant_id, "report_pdf", feature=FEATURE, report_id=str(report_id))
        return {
            "summary": summary,
            "pdf": pdf,
            "pdf_path": pdf_path,
            "panel_base": panel_base,
        }

    async def _deltas(
        self,
        current: dict[str, Any],
        tenant_id: UUID,
        period: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, int]:
        """Change vs the previous equivalent window, for the p1 tiles.

        A second collect over the prior window — the same millisecond COUNTs,
        not a stored comparison, so a regenerated report compares against the
        same numbers a re-run would show.
        """
        from app.services.growth.reports import queries

        length = end - start
        try:
            prev = await queries.collect(
                self._c,
                tenant_id,
                period=period,
                start=start - length,
                end=start,
                months_back=0,
            )
        except Exception:
            logger.warning("growth_report_delta_failed", exc_info=True)
            return {}
        return _delta_between(prev, current)

    # --------------------------------------------------------------- summary

    async def _day_findings(
        self, tenant_id: UUID, settings: dict[str, Any]
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """What the other agents appended to the tenant's run-context day.

        The whole point of the run context store: instead of re-querying every
        surface, the report reads the day's working memory the swarm left
        behind. Guarded because the numbers stand on their own — an empty or
        unreadable day costs the summary its agent notes, never the report.
        """
        from app.services.growth.reports.windows import tenant_today

        try:
            return await self._c.growth.run_context.read_day(
                tenant_id, tenant_today(settings)
            )
        except Exception:
            logger.warning(
                "growth_report_findings_read_failed",
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            return {}

    async def _summarise(
        self,
        tenant_id: UUID,
        numbers: dict[str, Any],
        findings: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    ) -> str:
        from app.services.growth.metering import meter_llm
        from app.services.growth.reports import render

        fallback = render.fallback_summary(numbers)
        notes = _findings_block(findings or {})
        if not has_content_for_llm(numbers) and not notes:
            return fallback
        try:
            llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "utility")
        except Exception:
            logger.warning("growth_report_no_provider", exc_info=True)
            return fallback
        if llm is None:
            return fallback

        import json

        from app.adapters.llm.base import Message

        evidence = json.dumps(numbers, default=str)[:8000]
        if notes:
            # the notes ride in the evidence as well as the prompt: the
            # grounding judge compares the draft against the evidence, and a
            # summary that quotes a finding's number must be able to point at
            # it there, or block mode refuses a grounded sentence
            evidence = f"{evidence}\n\nAGENT NOTES (today):\n{notes}"
        system = await self._system_prompt(tenant_id)
        try:
            completion = await asyncio.wait_for(
                llm.complete(
                    [
                        Message(role="system", content=system),
                        Message(
                            role="user",
                            content=_PROMPT.format(numbers=evidence),
                        ),
                    ],
                    max_tokens=SUMMARY_MAX_TOKENS,
                ),
                timeout=SUMMARY_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("growth_report_llm_failed", exc_info=True)
            return fallback

        await meter_llm(
            tenant_id,
            completion.usage,
            feature=FEATURE,
            agent_key=AGENT_KEY,
            model=getattr(completion, "model", ""),
        )
        draft = (completion.text or "").strip()
        if not draft:
            return fallback
        verdict = await self._c.growth_grounding.check(
            tenant_id, agent_key=AGENT_KEY, draft=draft, evidence=evidence
        )
        if not verdict.may_send:
            # block mode refused the summary: the numbers stand on their own,
            # so the report ships the deterministic lines instead of no words
            logger.info(
                "growth_report_summary_blocked",
                tenant_id=str(tenant_id),
                state=verdict.state,
            )
            await self._c.growth.audit.record(
                tenant_id,
                agent=AGENT_KEY,
                action="summary_blocked_ungrounded",
                input={},
                output={"draft": draft[:1000], **verdict.as_meta()},
                reason=verdict.reason or "the grounding judge refused the summary",
            )
            return fallback
        return draft

    # -------------------------------------------------------------- delivery

    async def _deliver(
        self,
        tenant_id: UUID,
        report_id: UUID,
        summary: str,
        pdf: bytes,
        pdf_path: str,
        panel_base: str,
    ) -> dict[str, int]:
        """WhatsApp utility template + PDF link, Telegram + PDF + buttons, panel.

        The panel delivery is the ledger row itself. Slack is deliberately not
        a leg: the spec names WhatsApp, Telegram and the panel, and a fourth
        copy is noise, not coverage.
        """
        from app.services.growth.metering import meter

        panel_url = f"{panel_base}/growth/reports?id={report_id}" if panel_base else ""
        try:
            pdf_url = await self._c.storage.signed_url(pdf_path, ttl_seconds=7 * 86400)
        except Exception:
            logger.warning("growth_report_signed_url_failed", exc_info=True)
            pdf_url = ""
        delivered = {"whatsapp": 0, "telegram": 0}

        try:
            mode = await self._deliver_whatsapp(tenant_id, summary, pdf_url, panel_url)
            if mode:
                delivered["whatsapp"] = 1
            if mode == "template":
                await meter(
                    tenant_id,
                    "whatsapp_conversation_utility",
                    feature=FEATURE,
                    report_id=str(report_id),
                )
        except Exception:
            logger.warning("growth_report_whatsapp_failed", exc_info=True)
        try:
            if await self._deliver_telegram(
                tenant_id, report_id, summary, pdf, pdf_url, panel_url
            ):
                delivered["telegram"] = 1
        except Exception:
            logger.warning("growth_report_telegram_failed", exc_info=True)
        return delivered

    async def _deliver_whatsapp(
        self, tenant_id: UUID, summary: str, pdf_url: str, panel_url: str
    ) -> str:
        """Owner ping: approved utility template when one exists, else text.

        Returns which leg fired — ``"template"``, ``"text"`` or ``""`` for no
        delivery. The caller meters a utility conversation only on the template
        leg: a free-form reply inside the 24h window is not one, and a cost
        line that does not reconcile against Meta's invoice is worse than none.
        """
        from app.adapters.channels.whatsapp import WhatsAppClient
        from app.services.growth.reports import render

        integration = await self._c.whatsapp_repo.active_for_tenant(tenant_id)
        if integration is None:
            return ""
        record = await self._c.whatsapp_repo.get_access_token_record(
            tenant_id, integration.id
        )
        if record is None:
            return ""
        owner_rows = await self._c.growth.integrations.list(tenant_id)
        recipient = ""
        for row in owner_rows:
            if row.get("provider") == "whatsapp" and row.get("status") == "active":
                recipient = str(row.get("external_ref") or "")
                break
        if not recipient:
            return ""

        from app.core.security import decrypt_secret

        try:
            token = decrypt_secret(
                str(record.ciphertext), self._c.settings.ENCRYPTION_KEY
            )
        except Exception:
            logger.warning("growth_report_whatsapp_secret_undecryptable", exc_info=True)
            return ""

        settings = self._c.settings
        client = WhatsAppClient(
            token,
            str(integration.phone_number_id),
            version=settings.WHATSAPP_GRAPH_VERSION,
            timeout=settings.HTTP_TIMEOUT_SECONDS,
            max_message_length=settings.WHATSAPP_MAX_MESSAGE_LENGTH,
        )
        try:
            template = await self._c.growth.wa_templates.approved(
                tenant_id, WA_TEMPLATE_NAME, "en"
            )
            if template is not None:
                await client.send_template(
                    recipient,
                    str(template["name"]),
                    str(template.get("language") or "en"),
                    [
                        {
                            "type": "body",
                            "parameters": [
                                {
                                    "type": "text",
                                    "text": render.whatsapp_template_param(summary),
                                }
                            ],
                        }
                    ],
                )
                mode = "template"
            else:
                await client.send_text(
                    recipient, render.whatsapp_text(summary, pdf_url, panel_url)
                )
                mode = "text"
            if pdf_url:
                # the spec asks for the PDF itself, not only a link; a failure
                # here leaves the summary delivered rather than losing the ping
                try:
                    await client.send_document(
                        recipient,
                        pdf_url,
                        filename="report.pdf",
                        caption=panel_url,
                    )
                except Exception:
                    logger.warning(
                        "growth_report_whatsapp_document_failed", exc_info=True
                    )
            return mode
        finally:
            await client.close()

    async def _deliver_telegram(
        self,
        tenant_id: UUID,
        report_id: UUID,
        summary: str,
        pdf: bytes,
        pdf_url: str,
        panel_url: str,
    ) -> bool:
        """Owner ping on Telegram: message with buttons, then the PDF."""
        from app.adapters.channels.telegram import TelegramApprovalNotifier
        from app.services.growth.reports import render

        rows = await self._c.growth.integrations.list(tenant_id)
        telegram = next(
            (
                row
                for row in rows
                if row.get("provider") == "telegram" and row.get("status") == "active"
            ),
            None,
        )
        if telegram is None or not self._c.settings.TELEGRAM_ENABLED:
            return False
        token = await self._decrypt_secret(
            tenant_id, telegram.get("secret_ref"), "telegram_bot"
        )
        if not token and self._c.settings.TELEGRAM_BOT_TOKEN:
            # provider mode: the platform bot sends for a tenant that bound a
            # chat with /start and stored no token of their own
            token = self._c.settings.TELEGRAM_BOT_TOKEN
        chat_id = str(telegram.get("external_ref") or "")
        if not token or not chat_id:
            return False
        notifier = TelegramApprovalNotifier(
            token,
            chat_id,
            lambda _request: render.telegram_report_payload(
                summary, pdf_url, panel_url
            ),
            timeout=self._c.settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS,
        )
        try:
            receipt = await notifier.send_payload(
                render.telegram_report_payload(summary, pdf_url, panel_url)
            )
            if not receipt.delivered:
                return False
            doc_receipt = await notifier.send_document(
                "growth-report.pdf", pdf, caption="Full report, with charts."
            )
            return bool(doc_receipt.delivered)
        finally:
            await notifier.close()

    async def _decrypt_secret(
        self, tenant_id: UUID, ref: Any, provider: str
    ) -> str | None:
        if not ref:
            return None
        from sqlalchemy import text

        from app.core.db import tenant_scope
        from app.core.security import decrypt_secret

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT ciphertext FROM tenant_secrets "
                        "WHERE tenant_id = :tid AND ref = :ref AND provider = :provider"
                    ),
                    {"tid": tenant_id, "ref": str(ref), "provider": provider},
                )
            ).mappings().first()
        if row is None:
            return None
        try:
            return decrypt_secret(
                str(row["ciphertext"]), self._c.settings.ENCRYPTION_KEY
            )
        except Exception:
            logger.warning("growth_report_secret_undecryptable", exc_info=True)
            return None

    def _panel_base(self) -> str:
        settings = self._c.settings
        return str(
            settings.DASHBOARD_URL or settings.PUBLIC_BASE_URL or ""
        ).rstrip("/")


# ------------------------------------------------------------------ telegram


def telegram_command(update: dict[str, Any]) -> str | None:
    """The owner-facing bot command in a Telegram update, if any.

    The interactive surface (P6) is menu-keyboard driven, so this recognises
    both forms: the slash command (``/report``, with or without the ``@botname``
    suffix Telegram adds in groups) and the reply-keyboard label (``Report``) —
    they map to the same handler and must not drift. Anything else — stray
    text, edited non-commands — is None, and the ingress answers 200 without
    acting.
    """
    message = update.get("message") or update.get("edited_message") or {}
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    first = text.strip().split()[0] if text.strip() else ""
    token = first.split("@")[0].lower()
    command = token.lstrip("/")
    if command in OWNER_COMMANDS:
        return OWNER_COMMANDS[command]
    # a reply-keyboard label: exact text match, case-insensitive, no suffix
    # parsing — Telegram sends the label verbatim
    return MENU_KEY_COMMANDS.get(text.strip().lower())


# /start and /menu are the same thing: the entry point that shows the keyboard.
OWNER_COMMANDS: dict[str, str] = {
    "report": "report",
    "menu": "menu",
    "start": "menu",
    "insights": "insights",
    "pending": "approvals",
    "approvals": "approvals",
    "profile": "profile",
    "settings": "settings",
    "help": "help",
}

# The reply-keyboard labels (`/menu` renders them) to the commands they run.
MENU_KEY_COMMANDS: dict[str, str] = {
    label.lower(): command
    for label, command in (
        ("Report", "report"),
        ("Insights", "insights"),
        ("Approvals", "approvals"),
        ("Profile", "profile"),
        ("Settings", "settings"),
        ("Help", "help"),
    )
}


def has_content_for_llm(numbers: dict[str, Any]) -> bool:
    """Whether a summary LLM call is worth its tokens.

    An empty window gets the deterministic lines without paying for a model to
    restate zeros — the same reason the PDF renders one section for no data.
    """
    from app.services.growth.reports import render

    return render.has_activity(numbers)


def _delta_between(prev: dict[str, Any], current: dict[str, Any]) -> dict[str, int]:
    prev_funnel = prev.get("funnel") or {}
    cur_funnel = current.get("funnel") or {}

    def mailed(funnel: dict[str, Any]) -> int:
        return sum(
            int(item.get("count") or 0) for item in funnel.get("outbound") or []
        )

    return {
        "leads": int(cur_funnel.get("leads_created") or 0)
        - int(prev_funnel.get("leads_created") or 0),
        "mailed": mailed(cur_funnel) - mailed(prev_funnel),
        "replies": int(cur_funnel.get("inbound") or 0)
        - int(prev_funnel.get("inbound") or 0),
    }
