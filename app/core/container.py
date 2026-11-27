"""Composition root: every adapter is constructed here, once, at startup.

This is the only module that knows which concrete implementation stands behind
each port. Services receive already-built objects, which is what keeps provider
names out of `services/` and `domain/` and makes a vendor swap a config change.

Built lazily and cached: a deployment with no OCR configured should not pay to
construct an OCR client, and a request should never trigger construction.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class _AsyncClientCloser:
    """Adapts an httpx client's `aclose()` to the `close()` shutdown expects."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def close(self) -> None:
        await self._client.aclose()


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._closeables: list[Any] = []

    def _track(self, obj: Any) -> Any:
        """Remember anything holding a connection pool, so shutdown can close it."""
        if hasattr(obj, "close"):
            self._closeables.append(obj)
        return obj

    @cached_property
    def embeddings(self) -> Any:
        from app.adapters.embeddings.dense import (
            FakeEmbeddings,
            JinaEmbeddings,
            OpenAIEmbeddings,
        )

        backend = self.settings.EMBEDDING_BACKEND
        if backend == "jina":
            return self._track(JinaEmbeddings(self.settings))
        if backend == "openai":
            return self._track(OpenAIEmbeddings(self.settings))
        return FakeEmbeddings()

    @cached_property
    def sparse_encoder(self) -> Any:
        from app.adapters.embeddings.bm25 import BM25Encoder

        return BM25Encoder()

    @cached_property
    def vector_store(self) -> Any:
        from app.adapters.vector.memory import MemoryVectorStore

        if self.settings.VECTOR_BACKEND == "qdrant":
            from app.adapters.vector.qdrant import QdrantVectorStore

            return self._track(QdrantVectorStore(self.settings))
        return MemoryVectorStore()

    @cached_property
    def storage(self) -> Any:
        from app.adapters.storage.base import LocalStorage, SupabaseStorage

        if self.settings.STORAGE_BACKEND == "supabase":
            return self._track(
                SupabaseStorage(
                    self.settings.SUPABASE_URL,
                    self.settings.SUPABASE_KEY,
                    self.settings.SUPABASE_BUCKET,
                )
            )
        return LocalStorage(self.settings.LOCAL_STORAGE_DIR)

    @cached_property
    def ocr(self) -> Any:
        """None is a valid outcome — see `build_ocr_chain`.

        A deployment with no OCR still ingests; scanned pages are marked degraded
        rather than failing the document.
        """
        from app.adapters.ocr.factory import build_ocr_chain

        return build_ocr_chain(self.settings)

    @cached_property
    def llm(self) -> Any:
        from app.adapters.llm.factory import build_from_settings

        return self._track(
            build_from_settings(self.settings, self.settings.DEFAULT_LLM_MODEL)
        )

    @cached_property
    def utility_llm(self) -> Any:
        """Cheaper provider for utility calls: groundedness check, rolling
        summary, small-talk fallback, intent classifier.

        `UTILITY_LLM_MODEL` empty means "no separate tier" and returns the
        managed provider, so the setting costs nothing until it is set. A model
        set with no platform key for the deployment's backend also degrades to
        the managed provider rather than answering with a 401.
        """
        from app.adapters.llm.factory import build_from_settings, platform_key

        model = self.settings.UTILITY_LLM_MODEL.strip()
        if not model or model == self.settings.DEFAULT_LLM_MODEL:
            return self.llm
        if not platform_key(self.settings, self.settings.LLM_BACKEND):
            return self.llm
        return self._track(build_from_settings(self.settings, model))

    @cached_property
    def queue(self) -> Any:
        from app.adapters.queue.base import LocalQueue

        if self.settings.QUEUE_BACKEND == "qstash":
            from app.adapters.queue.qstash import QStashQueue

            return self._track(
                QStashQueue(
                    self.settings.QSTASH_TOKEN, 
                    self.settings.PUBLIC_BASE_URL,
                    base_url=self.settings.QSTASH_URL
                )
            )
        return LocalQueue()

    @cached_property
    def parser_registry(self) -> Any:
        from app.adapters.parsers.registry import ParserRegistry

        return ParserRegistry()

    @cached_property
    def documents(self) -> Any:
        from app.adapters.postgres.document_repo import DocumentRepository

        return DocumentRepository(storage=self.storage)

    @cached_property
    def conversations(self) -> Any:
        from app.adapters.postgres.conversation_repo import ConversationRepository

        return ConversationRepository()

    @cached_property
    def whatsapp_repo(self) -> Any:
        from app.adapters.postgres.whatsapp_repo import WhatsAppRepository

        return WhatsAppRepository()

    @cached_property
    def service_window(self) -> Any:
        """Meta's 24-hour rule. Checked before a send, not after a rejection."""
        from app.services.channels.window import ServiceWindow

        return ServiceWindow(
            self.whatsapp_repo,
            self.cache,
            ttl_seconds=self.settings.WHATSAPP_WINDOW_CACHE_TTL_SECONDS,
        )

    @cached_property
    def whatsapp(self) -> Any:
        from app.services.channels.whatsapp import WhatsAppService

        return WhatsAppService(self)

    @cached_property
    def indexer(self) -> Any:
        from app.services.ingestion.indexer import Indexer

        return Indexer(
            self.vector_store,
            self.embeddings,
            self.sparse_encoder,
            collection_prefix=self.settings.QDRANT_COLLECTION_PREFIX,
            batch_size=self.settings.EMBED_BATCH_SIZE,
        )

    @cached_property
    def contextualiser(self) -> Any:
        """Optional index-time chunk contextualiser. Uses the managed LLM and the
        cache so a re-index is free."""
        from app.services.ingestion.contextualiser import Contextualiser

        return Contextualiser(self.llm, self.cache)

    @cached_property
    def pipeline(self) -> Any:
        from app.services.ingestion.pipeline import DocumentPipeline

        return DocumentPipeline(
            self.indexer,
            self.parser_registry,
            self.documents,
            ocr_provider=self.ocr,
            contextualiser=self.contextualiser,
        )

    @cached_property
    def gate(self) -> Any:
        from app.services.ingestion.gate import UploadGate

        return UploadGate(self.settings)

    @cached_property
    def cache(self) -> Any:
        from app.adapters.cache.base import build_cache

        return self._track(build_cache(self.settings))

    @cached_property
    def rate_limiter(self) -> Any:
        from app.adapters.cache.base import RateLimiter

        return RateLimiter(self.cache)

    @cached_property
    def telemetry(self) -> Any:
        from app.adapters.telemetry.base import build_telemetry

        # the conversation repo owns `usage_events`, so metering survives a
        # tracing-vendor outage
        return self._track(build_telemetry(self.settings, self.conversations))

    @cached_property
    def reranker(self) -> Any:
        """None is a valid outcome — retrieval keeps fusion order without it."""
        from app.adapters.rerank.base import build_reranker

        provider = build_reranker(self.settings)
        return self._track(provider) if provider is not None else None

    @cached_property
    def retrieval(self) -> Any:
        from app.services.retrieval.search import RetrievalService

        return RetrievalService(
            self.vector_store,
            self.embeddings,
            self.sparse_encoder,
            reranker=self.reranker,
            collection_prefix=self.settings.QDRANT_COLLECTION_PREFIX,
            # document-level facets (tags/collection/metadata) are resolved live
            # from Postgres rather than read from the chunk payload, so retrieval
            # needs the repo. see split_filters in services/retrieval/search.py
            documents=self.documents,
        )

    @cached_property
    def web_search(self) -> Any:
        """None when unconfigured — an agent's web-search tool then yields no web
        evidence, the same graceful-off posture as an unconfigured reranker."""
        from app.adapters.tools.web_search import build_web_search

        provider = build_web_search(self.settings)
        return self._track(provider) if provider is not None else None

    @cached_property
    def model_resolver(self) -> Any:
        """Turns an agent's ModelConfig into the provider for a request.

        Owns a cache of any per-agent providers it builds; they hold httpx
        clients, so they are tracked here for shutdown close.
        """
        from app.services.agent.model_resolver import ModelResolver

        return ModelResolver(self.settings, self.llm, self._track)

    @cached_property
    def agent(self) -> Any:
        from app.services.agent.runner import AgentRunner

        return AgentRunner(
            self.retrieval,
            self.llm,
            conversations=self.conversations,
            documents=self.documents,
            telemetry=self.telemetry,
            web_search=self.web_search,
            llm_resolver=self.model_resolver,
            utility_llm=self.utility_llm,
            cache=self.cache,
            settings=self.settings,
            # without this the runner's `self._actions is None` short-circuit
            # collapses every turn's toolset to empty, so no action is ever
            # dispatched from the chat path however the agent switches are set
            actions=self.action_service,
            # same reasoning as `actions` above: the runner's `self._procedures`
            # is what the procedure loop reads, and leaving it None disables
            # trigger matching entirely however PROCEDURES_ENABLED is set
            procedures=self.procedure_service,
            identity=self.identity_service,
            # without this every escalation trigger stays inert: the runner is the
            # only thing that sees a low-confidence or ungrounded answer, and with
            # no service to hand it to it simply returns the answer and forgets
            escalation=self.escalation,
            # the tenant-facts block for chat system prompts. None degrades to
            # today's prompt, same fail-open posture as identity above
            agent_prompts=self.agent_prompts,
        )

    @cached_property
    def source_repo(self) -> Any:
        from app.adapters.postgres.source_repo import SourceRepository

        return SourceRepository()

    @cached_property
    def page_fetcher(self) -> Any:
        """The crawler's plain-HTTP door: robots.txt, sitemaps and PDF bytes.

        One client for the whole process so a 400-page crawl reuses connections,
        and tracked for shutdown like every other httpx holder. Nothing else in the
        codebase may open a socket to a tenant-supplied URL — the SSRF checks and
        the redirect walk live inside this object.
        """
        from app.services.crawl.fetch import build_page_fetcher

        return self._track(build_page_fetcher(self.settings, self.cache))

    @cached_property
    def site_crawler(self) -> Any:
        """The crawler's browser door: crawl4ai behind page reads and site walks.

        One Chromium for the whole process, started lazily on the first read or
        discovery so a sitemap-only source never pays for it. The browser opens
        its own sockets, so the SSRF checks it cannot inherit from the httpx
        path live inside the adapter instead — per page before a read, per
        distinct host during a site walk.
        """
        from app.services.crawl.library import build_site_crawler

        return self._track(build_site_crawler(self.settings))

    @cached_property
    def renderer(self) -> Any:
        """None is a valid outcome — an SPA is then recorded as
        `javascript_rendered` rather than rendered, the same graceful-off posture
        as an unconfigured reranker."""
        from app.adapters.render.base import build_renderer

        provider = build_renderer(self.settings)
        return self._track(provider) if provider is not None else None

    @cached_property
    def crawl_engine(self) -> Any:
        from app.services.crawl.engine import CrawlEngine

        return CrawlEngine(
            self.settings,
            self.source_repo,
            self.documents,
            self.storage,
            self.queue,
            self.cache,
            renderer=self.renderer,
            indexer=self.indexer,
            fetcher=self.page_fetcher,
            site_crawler=self.site_crawler,
        )

    @cached_property
    def sources(self) -> Any:
        from app.services.sources.service import SourceService

        return SourceService(self)

    @cached_property
    def snippets(self) -> Any:
        from app.services.sources.snippets import SnippetService

        return SnippetService(self)

    @cached_property
    def qa(self) -> Any:
        from app.services.sources.qa import QaService

        return QaService(self)

    # ----------------------------------------------------------------- actions

    @cached_property
    def _actions_http_client(self) -> Any:
        """A dedicated pool for outbound tenant actions.

        Its own client, not the shared one, for three reasons that are all
        security rather than tidiness. `follow_redirects=False` is mandatory here:
        following a redirect re-points the request after the SSRF check has
        already passed, which is the standard bypass. The timeouts are short
        because a visitor is waiting on the other end of this call. And a separate
        pool means one tenant's slow integration cannot exhaust the connections
        embedding and the LLM depend on.
        """
        import httpx

        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self.settings.ACTIONS_TIMEOUT_SECONDS, connect=5.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            headers={"User-Agent": self.settings.ACTIONS_USER_AGENT},
        )
        # httpx exposes aclose(), not close(), so _track would not register it.
        self._closeables.append(_AsyncClientCloser(client))
        return client

    @cached_property
    def secrets(self) -> Any:
        from app.adapters.db.actions_repo import ActionSecretResolver

        return ActionSecretResolver(self.settings)

    @cached_property
    def notifier(self) -> Any:
        """Outbound operator notifications, or None.

        None is a supported state: no notifier ships yet, and an action that
        cannot notify an operator has still done its work. Library handlers treat
        it the same way the crawler treats a missing renderer.
        """
        return None

    @cached_property
    def actions_repo(self) -> Any:
        from app.adapters.db.actions_repo import ActionsRepo

        return ActionsRepo()

    @cached_property
    def action_executor(self) -> Any:
        from app.core.breaker import get as get_breaker
        from app.services.actions.executor import ActionExecutor

        return ActionExecutor(
            http=self._actions_http_client,
            secrets=self.secrets,
            breaker=get_breaker("actions"),
            rate_limiter=self.rate_limiter,
            settings=self.settings,
            # without this the executor cannot tell "the row has no external_id"
            # from "there is no row", and a `{{contact.*}}` hole would have to
            # refuse every time. It is the only supplier of that namespace.
            identity=self.identity_service,
        )

    @cached_property
    def action_service(self) -> Any:
        """Always constructed, even with the platform switch off.

        Graceful-off lives in `ToolSet.resolve`, not here: a None service would
        force every call site to branch, and the branch that gets forgotten is the
        one that raises on a live chat request.
        """
        from app.services.actions.service import ActionService

        return ActionService(
            repo=self.actions_repo,
            executor=self.action_executor,
            notifier=self.notifier,
            settings=self.settings,
        )

    # -------------------------------------------------------------- procedures

    @cached_property
    def procedures_repo(self) -> Any:
        from app.adapters.postgres.procedures_repo import ProceduresRepo

        return ProceduresRepo()

    @cached_property
    def procedure_service(self) -> Any:
        """Always constructed, like the action service and for the same reason.

        `PROCEDURES_ENABLED` is checked where a procedure is authored or
        matched, not here. A None service would push a branch into every call
        site, and the one that gets forgotten raises on a live chat turn.

        `actions` is passed because a template install sometimes has to scaffold
        the action a procedure step calls. The service holds it optionally, so a
        deployment with the actions engine off still authors procedures.
        """
        from app.services.procedures.service import ProcedureService

        return ProcedureService(
            repo=self.procedures_repo,
            actions=self.action_service,
            settings=self.settings,
        )

    # ------------------------------------------------------- verified identity

    @cached_property
    def contacts_repo(self) -> Any:
        from app.adapters.db.contacts_repo import ContactsRepo

        return ContactsRepo()

    @cached_property
    def identity_service(self) -> Any:
        """Always constructed, even with verification off.

        Graceful-off lives in `IdentityService.enabled`, not here: a None service
        would force the session endpoint to branch, and an unconfigured tenant
        must still get an anonymous session rather than an error.
        """
        from app.services.contacts import IdentityService

        return IdentityService(self)

    # --------------------------------------------- escalation + email channel

    @cached_property
    def escalation_repo(self) -> Any:
        from app.adapters.postgres.escalation_repo import EscalationRepository

        return EscalationRepository()

    @cached_property
    def escalation(self) -> Any:
        """Always constructed. Graceful-off is per agent, not per deployment.

        The notifiers are deliberately *not* built here: a Slack webhook URL is a
        tenant secret and the destination list is tenant config, so a cached
        notifier would hold a decrypted credential or serve a stale one.
        """
        from app.services.escalation.service import EscalationService

        return EscalationService(self)

    @cached_property
    def email_repo(self) -> Any:
        from app.adapters.postgres.email_repo import EmailRepository

        return EmailRepository()

    @cached_property
    def email(self) -> Any:
        """The EmailProvider, or None when EMAIL_BACKEND=none.

        None is the normal unconfigured state, exactly as it is for the reranker
        and OCR: callers check for it and degrade rather than branch on a backend
        name. Nothing here reads tenant data, so a deployment with no email
        configured pays nothing for the channel existing.
        """
        if self.settings.EMAIL_BACKEND != "resend":
            return None
        if not self.settings.RESEND_API_KEY:
            logger.warning("email_backend_missing_api_key")
            return None
        from app.adapters.channels.email_resend import ResendEmail

        return self._track(
            ResendEmail(
                self.settings.RESEND_API_KEY,
                timeout=self.settings.HTTP_TIMEOUT_SECONDS,
            )
        )

    @cached_property
    def email_service(self) -> Any:
        """Named apart from `email` because that name belongs to the provider port.

        `whatsapp` / `whatsapp_repo` could use the short name for the service
        because Meta's client is not a shared port; `EmailProvider` is.
        """
        from app.services.channels.email import EmailService

        return EmailService(self)

    # ---------------------------------------------------------------- growth

    @cached_property
    def growth(self) -> Any:
        """Repositories for the growth log DB.

        One handle rather than eight properties: nearly every growth service
        needs two or three of these together, and eight constructor arguments
        that all point at the same database is wiring, not design. Holds no
        connection of its own — every method opens its own scope — so it is not
        tracked for close.
        """
        from app.adapters.postgres.growth_repo import GrowthRepository

        return GrowthRepository()

    @cached_property
    def growth_profile(self) -> Any:
        """Business profile and settings. Always constructed.

        Graceful-off for growth is per tenant (the profile's ``tenant_type``) and
        per deployment (``GROWTH_ENABLED`` gates the *scheduler*), never per
        service — reading settings has to work even when nothing is acting on
        them, or the settings screen cannot be used to turn the feature on.
        """
        from app.services.growth.profile import ProfileService

        return ProfileService(self)

    @cached_property
    def approvals(self) -> Any:
        """The HITL service.

        The notifiers are deliberately *not* built here, for the reason
        ``escalation`` states: a Telegram bot token and a Slack webhook are
        tenant secrets and the destination list is tenant config, so a cached
        notifier would hold a decrypted credential or serve a stale one.
        """
        from app.services.approvals.service import ApprovalService

        return ApprovalService(self)

    @cached_property
    def growth_alerts(self) -> Any:
        from app.services.growth.alerts import AlertService

        return AlertService(self)

    @cached_property
    def growth_dispatch(self) -> Any:
        from app.services.growth.dispatch import DispatchService

        return DispatchService(self)

    @cached_property
    def growth_compliance(self) -> Any:
        from app.services.growth.compliance import ComplianceService

        return ComplianceService(self)

    @cached_property
    def growth_grounding(self) -> Any:
        """The per-agent groundedness judge for growth deliverables."""
        from app.services.growth.grounding import GroundingService

        return GroundingService(self)

    @cached_property
    def growth_providers(self) -> Any:
        """Resolves which lead-data vendor a given tenant's run should call.

        Stateless, so caching it is free; the *adapters* it hands back are built
        per call when the key is a tenant secret, which is the rule the
        approval notifiers already follow.
        """
        from app.services.growth.providers import ProviderResolver

        return ProviderResolver(self)

    @cached_property
    def apollo(self) -> Any:
        """The platform's own Apollo account, or None when unconfigured.

        None is the ordinary state, exactly as it is for the reranker, OCR and
        web search: research falls back to web-only, which is the base case and
        not a degraded one. A tenant with their own Apollo account overrides
        this per tenant through `growth_providers`.
        """
        from app.adapters.leads.apollo import build_apollo

        provider = build_apollo(
            self.settings.APOLLO_API_KEY, timeout=self.settings.HTTP_TIMEOUT_SECONDS
        )
        return self._track(provider) if provider is not None else None

    @cached_property
    def hunter(self) -> Any:
        """The platform's own Hunter account, or None when unconfigured."""
        from app.adapters.leads.hunter import build_hunter

        provider = build_hunter(
            self.settings.HUNTER_API_KEY, timeout=self.settings.HTTP_TIMEOUT_SECONDS
        )
        return self._track(provider) if provider is not None else None

    @cached_property
    def email_verifier(self) -> Any:
        """The configured email verifier, or None.

        Unconfigured means addresses are stored unverified and treated the way a
        `risky` verdict is treated — sendable, and visibly less than confirmed.
        Refusing to send without a verifier would stop outbound on a deployment
        that simply never bought one.
        """
        from app.adapters.leads.verifier import build_verifier

        provider = build_verifier(
            self.settings.EMAIL_VERIFIER_BACKEND,
            self.settings.EMAIL_VERIFIER_API_KEY,
            timeout=self.settings.HTTP_TIMEOUT_SECONDS,
        )
        return self._track(provider) if provider is not None else None

    @cached_property
    def growth_senders(self) -> Any:
        """Customer-facing channel senders, by channel name.

        This is the attribute `DispatchService._send_all` looks for. Its absence
        is what made Stage 1's dispatcher defer every due message instead of
        sending it; supplying it is what completes the send path, without the
        scheduler, the cap or the lock changing at all.
        """
        from app.services.growth.senders import SenderRegistry

        return SenderRegistry(self)

    @cached_property
    def growth_research(self) -> Any:
        from app.services.growth.research import ResearchService

        return ResearchService(self)

    @cached_property
    def growth_profile_agent(self) -> Any:
        """The profile agent (P4): fills the business profile from the crawl.

        Always constructed, like ``growth_profile`` itself — the agent's own
        guards (source match, once-only) are what keep an unconfigured or
        already-filled tenant quiet, not the container's laziness.
        """
        from app.services.growth.profile_agent import ProfileAgentService

        return ProfileAgentService(self)

    @cached_property
    def growth_coldmail(self) -> Any:
        from app.services.growth.coldmail import ColdMailService

        return ColdMailService(self)

    @cached_property
    def growth_replies(self) -> Any:
        """The reply classifier. Always constructed, like the email service.

        A None service would push a branch into the two channel services, and
        the branch that gets forgotten is the one that swallows a customer's
        reply.
        """
        from app.services.growth.replies import ReplyService

        return ReplyService(self)

    @cached_property
    def growth_followup(self) -> Any:
        from app.services.growth.followup import FollowUpService

        return FollowUpService(self)

    @cached_property
    def growth_inbound(self) -> Any:
        from app.services.growth.inbound import InboundService

        return InboundService(self)

    @cached_property
    def growth_justdial(self) -> Any:
        """JustDial enquiries, which arrive as mail on the email channel.

        Built beside `growth_inbound` because it feeds the same pipeline: only
        the fetch-and-parse half is its own, the qualify/ping/ack tail is
        `growth_inbound.ingest`.
        """
        from app.services.growth.justdial import JustDialService

        return JustDialService(self)

    @cached_property
    def growth_indiamart(self) -> Any:
        """IndiaMART enquiries, pulled from the seller account's lead feed.

        Like `growth_justdial`, everything past the fetch is the shared
        `growth_inbound.ingest` tail. The adapter itself is built per call from
        the tenant's stored key (see `app/services/growth/indiamart.py`), never
        cached here — one cached adapter would hold one tenant's credential.
        """
        from app.services.growth.indiamart import IndiaMARTService

        return IndiaMARTService(self)

    @cached_property
    def growth_reports(self) -> Any:
        from app.services.growth.reports.service import ReportService

        return ReportService(self)

    @cached_property
    def growth_stores(self) -> Any:
        """Store connections: Shopify + WooCommerce sync, webhooks, CSV import.

        Always constructed, like the other growth services: reading sync state
        has to work even when nothing is connected, or the Connections screen
        cannot be used to connect the first store.
        """
        from app.services.growth.stores import StoreSyncService

        return StoreSyncService(self)

    @cached_property
    def growth_campaigns(self) -> Any:
        """Part C campaigns: draft -> approval -> send -> attribution."""
        from app.services.growth.campaigns.service import CampaignService

        return CampaignService(self)

    @cached_property
    def growth_insights(self) -> Any:
        """Part B insights: four watchers, three drop bars, one daily digest."""
        from app.services.growth.insights.service import InsightsService

        return InsightsService(self)

    @cached_property
    def telegram_bot(self) -> Any:
        """The interactive owner bot behind the telegram ingress (P6).

        Like the notifiers the approvals service builds, the bot's Telegram
        credential is read per update and never cached here — the service is
        stateless by design: which keyboard a message carries is the whole
        conversation state.
        """
        from app.services.growth.telegram_bot import TelegramBotService

        return TelegramBotService(self)

    @cached_property
    def growth_crm(self) -> Any:
        """Stage 6 CRM sync: one-way push of leads and activity notes.

        Always constructed, like the other growth services: the links screen
        and the connect flow have to work before anything is connected.
        """
        from app.services.growth.crm.service import CrmService

        return CrmService(self)

    @cached_property
    def zoho_app(self) -> Any:
        """The platform's Zoho OAuth app, or None when unconfigured.

        The app, not a tenant's org: a bound adapter holds a tenant's grant and
        is built per call by `growth_crm._provider_for`, never cached. None is
        the ordinary state on a deployment without Zoho configured — the
        connect flow refuses with a clear message rather than building an
        authorize URL that cannot complete.
        """
        if not self.settings.ZOHO_CLIENT_ID or not self.settings.ZOHO_CLIENT_SECRET:
            return None
        return {
            "client_id": self.settings.ZOHO_CLIENT_ID,
            "client_secret": self.settings.ZOHO_CLIENT_SECRET,
        }

    @cached_property
    def hubspot_app(self) -> Any:
        """The platform's HubSpot OAuth app, or None when unconfigured."""
        if not self.settings.HUBSPOT_CLIENT_ID or not self.settings.HUBSPOT_CLIENT_SECRET:
            return None
        return {
            "client_id": self.settings.HUBSPOT_CLIENT_ID,
            "client_secret": self.settings.HUBSPOT_CLIENT_SECRET,
        }

    @cached_property
    def shopify(self) -> Any:
        """The platform's Shopify app config, or None when unconfigured.

        This is the *app* (OAuth + webhook verification), not a tenant's
        store: a bound per-store adapter holds a tenant secret, so it is built
        per call by `growth_stores.provider_for` and never cached — the same
        rule the approval notifiers follow. None is the ordinary state, exactly
        like the platform Apollo key: the connect flow refuses with a clear
        message instead of building a broken authorize URL.
        """
        if not self.settings.SHOPIFY_API_KEY or not self.settings.SHOPIFY_API_SECRET:
            return None
        from app.adapters.stores.shopify import SCOPES

        return {
            "api_key": self.settings.SHOPIFY_API_KEY,
            "scopes": SCOPES,
        }

    @cached_property
    def woocommerce(self) -> Any:
        """WooCommerce needs no platform key — every store brings its own
        REST keypair — so this is a stateless helper namespace, always present.

        Per-store adapters are built per call by `growth_stores.provider_for`
        (tenant secrets are never cached); this property exists so the vendor
        is one container line, like every other port, rather than a bare import
        at each call site.
        """
        from app.adapters.stores import woocommerce as _woo

        return _woo

    @cached_property
    def growth_builtin(self) -> Any:
        """The builtin-things agent's capability registry (P3).

        Stateless like the other growth services — the services it dispatches
        to do the stateful work — so caching it is free. Every call appends its
        outcome to the run context, which is P1's store, not this one.
        """
        from app.services.growth.builtin import BuiltinService

        return BuiltinService(self)

    @cached_property
    def agent_models(self) -> Any:
        """Per-agent LLM routing. The only door growth agents get a provider by.

        Cached because the service is stateless — the *resolutions* it hands back
        are cached in Redis with a short TTL, and the providers it builds are
        built per call. A provider cached here would outlive a staff model change
        by the life of the process.
        """
        from app.services.growth.modelconfig import ModelConfigService

        return ModelConfigService(self)

    @cached_property
    def agent_prompts(self) -> Any:
        """Agent definitions, tenant facts, render-at-invocation prompts.

        The read side of the registry (app/services/agents/): which agents
        exist, what their templates render to for a given tenant, and whether
        each is active. Cached like ``agent_models`` — stateless, and the fact
        base it builds is cached in Redis under its own version key, so a
        cached instance never holds a stale tenant.
        """
        from app.services.agents.service import AgentPromptService

        return AgentPromptService(self)

    @cached_property
    def orchestrator_agent(self) -> Any:
        """The owner's Telegram front door: answers from live data, with tools.

        Built like ``agent_prompts`` — stateless, cached, every tenant fact and
        credential resolved per call. The bot service treats a missing instance
        as "not wired" and keeps its old silent-ignore, so a construction
        failure here costs the chat channel, never the process.
        """
        from app.services.agents.orchestrator import OrchestratorAgent

        return OrchestratorAgent(self)

    async def aclose(self) -> None:
        for obj in reversed(self._closeables):
            try:
                await obj.close()
            except Exception:
                # shutdown is best-effort; one stuck client must not block the rest
                logger.warning("close_failed", target=type(obj).__name__, exc_info=True)
        self._closeables.clear()
