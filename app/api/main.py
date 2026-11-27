"""Application factory.

Adapters are constructed once at startup and closed once at shutdown. A request
never builds a client, so nothing pays connection setup on the hot path and
nothing leaks a pool on reload.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.core.config import Settings, get_settings
from app.core.container import Container
from app.core.db import dispose_engine, init_engine
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, request_id_var, tenant_id_var

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # fail at boot rather than on the first request that happens to need the
    # missing setting
    settings.validate_runtime()

    init_engine(settings)
    container = Container(settings)
    app.state.container = container

    # the local queue executes in-process, so it needs the handler that the
    # callback route provides in the hosted setup. keyed on the object's type
    # rather than on QUEUE_BACKEND: any backend that falls back to LocalQueue
    # would otherwise run with no handler and silently drop every job
    from app.adapters.queue.base import LocalQueue

    if isinstance(container.queue, LocalQueue):
        container.queue.set_handler(_make_local_handler(container))

    # LOCAL_CRON: the laptop/demo answer to "no QStash schedules exist here".
    # One in-process ticker fires the cron task bodies on their documented
    # cadences, so the growth swarm actually runs unattended locally instead of
    # waiting for pings that never come.
    local_cron_stop = None
    if settings.LOCAL_CRON:
        from app.api.internal.cron import start_local_cron

        local_cron_stop = start_local_cron(container)
        logger.info("local_cron_started", tasks="cron.LOCAL_CRON_INTERVALS")

    logger.info(
        "startup",
        environment=settings.ENVIRONMENT,
        vector=settings.VECTOR_BACKEND,
        queue=settings.QUEUE_BACKEND,
        storage=settings.STORAGE_BACKEND,
        llm=settings.LLM_BACKEND,
    )

    try:
        yield
    finally:
        if local_cron_stop is not None:
            local_cron_stop.set()
        if isinstance(container.queue, LocalQueue):
            # give in-flight ingestion a chance to finish rather than dropping it
            # on every deploy
            await container.queue.drain()
        await container.aclose()
        await dispose_engine()
        logger.info("shutdown")


def _make_local_handler(container: Container) -> Any:
    """Dev-mode job execution, reusing the same code path as the callback."""

    async def handle(job: Any) -> None:
        from app.api.internal.jobs import (
            GROWTH_JOB_KINDS,
            _crawl_wave,
            _delete_source,
            _growth,
            _ingest,
            _ingest_batch,
            _ingest_qa,
        )

        if job.kind == "ingest_document":
            await _ingest(container, job)
        elif job.kind == "ingest_documents":
            # the batch form the crawl engine enqueues; same branch shape as the
            # qstash callback so dev and prod agree on what a job does
            await _ingest_batch(container, job)
        elif job.kind == "crawl_wave":
            # Without this a crawl enqueued on the local queue is accepted and then
            # dropped, which looks exactly like a site that never responds.
            await _crawl_wave(container, job)
        elif job.kind == "delete_source":
            # deleting a source must reap its children on the local queue too,
            # or the pages stay searchable forever in dev while working in prod
            await _delete_source(container, job)
        elif job.kind == "ingest_qa":
            await _ingest_qa(container, job)
        elif job.kind == "whatsapp_reply":
            await container.whatsapp.process_reply(job.tenant_id, job.payload)
        elif job.kind == "email_reply":
            await container.email_service.process_reply(job.tenant_id, job.payload)
        elif job.kind == "escalation_notify":
            await container.escalation.notify(
                job.tenant_id, uuid.UUID(job.payload["escalation_id"])
            )
        elif job.kind in GROWTH_JOB_KINDS:
            # the growth kinds go through the same branch the qstash callback
            # uses, rather than a second copy here: a dev-only divergence in what
            # a job does is the hardest kind of bug to see, because both halves
            # look right on their own
            await _growth(container, job)

    return handle


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_JSON)

    app = FastAPI(
        title="AGENTS and AGENTS",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def cors(request: Request, call_next: Any) -> Response:
        """CORS for the dashboard plus the growth form bridge.

        The form snippet is embedded on tenant websites whose origins cannot be
        known statically, so the form endpoint is allowed for any origin:
        returning CORS headers lets a browser complete that exchange; it does
        not authenticate a caller or bypass the per-key and per-IP rate checks.
        """
        origin = request.headers.get("origin")
        path = request.url.path
        is_form_route = path == "/v1/growth/form"
        dashboard_origins = {item.rstrip("/") for item in settings.cors_origin_list}
        dashboard_origin = bool(origin and origin.rstrip("/") in dashboard_origins)
        allowed = bool(origin) and (is_form_route or dashboard_origin)

        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            if not allowed:
                return Response(status_code=403)
            return Response(status_code=204, headers=_cors_headers(origin, dashboard_origin))

        response = await call_next(request)
        if allowed:
            for key, value in _cors_headers(origin, dashboard_origin).items():
                response.headers[key] = value
        return response

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        """Attach a request id and clear tenant state.

        Contextvars are reset per request because the worker is reused; a leaked
        tenant id would mislabel another tenant's logs.
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request_id_var.set(request_id)
        tenant_id_var.set(None)

        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        """Baseline security response headers, on every response.

        Declared last, so it is the outermost HTTP middleware: it also stamps the
        responses the CORS and request-context layers and the AppError handler
        produce, since those sit inside it.

        The strict `default-src 'none'` CSP is scoped to non-HTML responses on
        purpose. This API also serves first-party HTML — the landing page, the
        unsubscribe page, and (off production) the Swagger UI — and each uses
        inline styles that `default-src 'none'` would blank out. Those pages are
        never framed cross-origin, so `X-Frame-Options: DENY` denies framing
        without a document CSP. JSON, the form snippet script and file downloads
        are not browsing contexts, so the strict policy costs them nothing and is
        one more layer if one is ever opened directly. `setdefault` throughout,
        so a route that needs a different value can set its own and keep it.
        """
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if settings.is_production:
            # only meaningful over TLS, which is production. Asserting it in dev,
            # usually plain http, would pin http failures in the browser's HSTS
            # cache and lock the developer out of localhost.
            headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        if "text/html" not in headers.get("content-type", ""):
            headers.setdefault(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        """One error shape for the whole API.

        Clients branch on `code`, which is stable; `message` is for humans and
        may change.
        """
        if exc.status_code >= 500:
            logger.error("request_failed", code=exc.code, message=exc.message, exc_info=True)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        """Never leak an internal message or traceback to a caller."""
        logger.error("unhandled_error", error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "an unexpected error occurred",
                }
            },
        )

    from app.api import a2a, growth_public, public
    from app.api.internal import cron, jobs
    from app.api.v1 import (
        actions,
        agents,
        analytics,
        audit,
        auth,
        chat,
        contacts,
        conversations,
        documents,
        escalations,
        graph,
        growth,
        integrations,
        integrations_email,
        keys,
        models,
        orchestrator_api,
        procedures,
        security,
        sources,
        usage,
    )
    from app.api.webhooks import email as email_webhooks
    from app.api.webhooks import ingress as ingress_webhooks
    from app.api.webhooks import whatsapp

    app.include_router(auth.router, prefix="/v1")
    app.include_router(agents.router, prefix="/v1")
    app.include_router(graph.router, prefix="/v1")
    app.include_router(keys.router, prefix="/v1")
    app.include_router(conversations.router, prefix="/v1")
    app.include_router(usage.router, prefix="/v1")
    app.include_router(analytics.router, prefix="/v1")
    app.include_router(audit.router, prefix="/v1")
    app.include_router(chat.router, prefix="/v1")
    app.include_router(orchestrator_api.router, prefix="/v1")
    app.include_router(orchestrator_api.a2a_router)
    app.include_router(documents.router, prefix="/v1")
    app.include_router(sources.router, prefix="/v1")
    app.include_router(security.router, prefix="/v1")
    app.include_router(integrations.router, prefix="/v1")
    app.include_router(integrations_email.router, prefix="/v1")
    app.include_router(models.router, prefix="/v1")
    app.include_router(actions.router, prefix="/v1")
    # mounted after actions because a procedure step calls an action, and a
    # template install can scaffold one; nothing in the ordering depends on it,
    # but the two read as one feature
    app.include_router(procedures.router, prefix="/v1")
    app.include_router(contacts.router, prefix="/v1")
    app.include_router(escalations.router, prefix="/v1")
    # the growth spine. Mounted whatever GROWTH_ENABLED says: the settings and
    # integrations screens are how a tenant configures the feature, and a router
    # that disappears when the feature is off cannot be used to turn it on. The
    # switch gates the *scheduler*, which is the half that acts.
    app.include_router(growth.router, prefix="/v1")
    app.include_router(whatsapp.router)
    # its own prefix is /webhooks/email, so it mounts unprefixed like whatsapp
    app.include_router(email_webhooks.router)
    # the unified ingress for every new inbound provider. One door, one
    # signature check, one enqueue — new providers never get a router of
    # their own, because five copies of those four steps is four too many
    app.include_router(ingress_webhooks.router)
    app.include_router(jobs.router)
    app.include_router(cron.router)

    # public, unauthenticated: the landing page and its redirects. registered
    # last so an explicit API route always wins over the catch-all landing page.
    # the three unauthenticated growth routes: the form snippet, the form
    # endpoint, and the unsubscribe page. Before the catch-all landing page
    # for the same reason every other explicit route is
    app.include_router(growth_public.router)
    # the A2A agent card: public, unauthenticated, JSON — an advertisement for
    # machine callers, mounted with the other public surfaces
    app.include_router(a2a.router)
    app.include_router(public.router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> dict[str, Any]:
        """Readiness depends on the database.

        The vector store and providers are deliberately not probed — a provider
        outage should shed that feature, not take the instance out of rotation.
        """
        from app.core.db import healthcheck

        return await healthcheck()

    _mount_dashboard(app)

    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built SPA under ``/app``.

    Under ``/app`` rather than at the root because ``/`` is the landing page
    (``app/api/public.py``) and ``/login`` is the Google callback that hands the
    session to the SPA in a URL fragment. A root-mounted SPA would swallow both,
    and sign-in would land on a 404 instead of the dashboard. ``/app`` is also
    where ``DASHBOARD_URL`` points, so the approval and escalation links resolve
    to a real screen.

    Registered after every router, so it can only ever answer a path the API did
    not claim.

    A checkout that never ran ``npm run build`` has no ``dashboard/dist`` — the
    UI is the vite dev server on :5173 there. That is a normal state, so it
    degrades to a explained 404 rather than a boot failure.
    """
    dist = Path("dashboard/dist")
    index = dist / "index.html"
    root = dist.resolve()

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    async def dashboard(path: str = "") -> Response:
        if path:
            candidate = (root / path).resolve()
            # the path is caller-controlled and may contain `..`; this check is
            # the only thing between it and any file the process can read
            if candidate.is_file() and candidate.is_relative_to(root):
                # vite fingerprints everything under assets/, so the bytes at a
                # given name never change and the response is safe to pin
                return FileResponse(
                    candidate,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"},
                )
            if path.startswith("assets/"):
                # a fingerprinted asset that is not on disk means a stale page
                # or a half-finished deploy; answering with index.html would
                # hand the browser HTML where it asked for JavaScript, and the
                # syntax error would point nowhere near the cause
                return JSONResponse(
                    status_code=404, content={"code": "asset_not_found", "path": path}
                )
        if not index.is_file():
            # a fresh response per call: the security middleware mutates the
            # headers of whatever it is handed, so a shared instance would
            # accumulate them
            return JSONResponse(
                status_code=404,
                content={
                    "code": "dashboard_not_built",
                    "message": (
                        "dashboard/dist is missing; build it with "
                        "`npm --prefix dashboard run build`"
                    ),
                },
            )
        # index.html names the fingerprinted bundles, so caching it would keep
        # serving a shell whose asset hashes a deploy has already replaced
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _cors_headers(origin: str | None, credentials: bool) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Origin": origin or "",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": (
            "Authorization, Content-Type, X-Request-ID, X-Tenant, X-Stepup-Grant"
        ),
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }
    if credentials:
        headers["Access-Control-Allow-Credentials"] = "true"
    return headers


app = create_app()
