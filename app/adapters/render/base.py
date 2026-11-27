"""Optional hosted browser rendering for JavaScript-only pages.

Off by default. A crawl that meets an SPA degrades to the sitemap plus a loud
warning; this adapter exists so a tenant who asked for `js_rendering` can get
those pages indexed anyway.

Why hosted and not Chromium in-process: a headless browser peaks at several
hundred MB RSS and adds cold-start weight to *every* wake of a scale-to-zero
process, including ordinary chat requests that will never render anything. So the
adapter POSTs a URL to a "give me the rendered HTML" endpoint over plain httpx —
one dependency, one socket, no browser in our image.

SSRF note, because this is the one place in the crawl package that talks to
something other than the tenant's own site: the socket here opens to
`CRAWL_RENDER_URL`, which is operator configuration and not tenant input, so the
urlguard checks are not applied to it (a self-hosted renderer on a private
address is a legitimate deployment). The *target* URL is tenant-reachable input,
so `assert_fetchable` runs on it before it is handed over — the renderer must not
become a way to launder a request to `169.254.169.254` through a third party that
would happily fetch it.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import (
    ConfigError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable
from app.domain.ports import PageRenderer, RenderResult

logger = get_logger(__name__)

# a render endpoint that has not answered in this long is not going to
RENDER_MAX_TIMEOUT_SECONDS = 90.0


class HttpPageRenderer:
    """Generic hosted-render backend.

    Speaks the shape every hosted renderer offers — `{"url": …}` in, HTML out —
    so ScrapingBee, Browserless, Scrapfly or a self-hosted Playwright box are a
    config change rather than a new adapter. The response is accepted as JSON
    (`html`/`content`/`data` key) or as a raw HTML body, because vendors differ
    and both are trivially detectable.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout: float = 60.0,
    ) -> None:
        headers = {"content-type": "application/json", "accept": "*/*"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            # vendors are split between bearer tokens and an x-api-key header;
            # sending both costs nothing and saves a per-vendor adapter
            headers["x-api-key"] = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = min(timeout, RENDER_MAX_TIMEOUT_SECONDS)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=10.0),
            headers=headers,
            # our own configured endpoint, but there is no reason to chase a
            # redirect from it either
            follow_redirects=False,
        )

    @property
    def name(self) -> str:
        return "http_render"

    async def render(self, url: str, *, timeout_seconds: float | None = None) -> RenderResult:
        """Rendered HTML for one page.

        Raises on transport failure or a non-2xx from the render endpoint; the
        caller records the page as `javascript_rendered` rather than retrying,
        because a render is metered and a retry loop is a billing event.
        """
        # tenant-reachable input, so it is validated before a third party is
        # asked to fetch it on our behalf. allow_private stays default-false
        # here regardless of settings: a renderer reaching a private address is
        # never something a crawl needs
        assert_fetchable(url)

        budget = min(timeout_seconds or self._timeout, RENDER_MAX_TIMEOUT_SECONDS)
        payload: dict[str, Any] = {
            "url": url,
            # the two names vendors use for "wait for the page to settle"
            "wait_for_network_idle": True,
            "timeout": int(budget * 1000),
        }

        try:
            response = await self._client.post(
                self._base_url, json=payload, timeout=httpx.Timeout(budget, connect=10.0)
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(self.name, "render timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(self.name, str(exc)) from exc

        if response.status_code >= 400:
            raise ProviderUnavailableError(
                self.name, f"{response.status_code}: {response.text[:300]}"
            )

        html, final_url = self._read_body(response, url)
        if not html.strip():
            raise ProviderUnavailableError(self.name, "render returned an empty document")
        return RenderResult(html=html, final_url=final_url, status=200)

    def _read_body(self, response: httpx.Response, requested_url: str) -> tuple[str, str]:
        """HTML and the URL the renderer says it ended on."""
        content_type = response.headers.get("content-type", "")
        if "json" in content_type.lower():
            try:
                body = response.json()
            except ValueError:
                return response.text, requested_url
            if isinstance(body, dict):
                for key in ("html", "content", "body", "data"):
                    value = body.get(key)
                    if isinstance(value, str) and value.strip():
                        final = body.get("url") or body.get("final_url")
                        return value, final if isinstance(final, str) and final else requested_url
            return response.text, requested_url
        return response.text, requested_url

    async def close(self) -> None:
        await self._client.aclose()


def build_renderer(settings: Settings) -> PageRenderer | None:
    """None is a valid outcome — the crawler then degrades to sitemap + warning.

    Mirrors `build_reranker`: an unset backend is off, a configured backend
    missing its URL is a warning and off, and an unknown backend is a
    `ConfigError` at startup rather than a surprise mid-crawl.
    """
    backend = settings.CRAWL_RENDER_BACKEND

    if backend == "none":
        return None

    if backend == "http":
        if not settings.CRAWL_RENDER_URL:
            logger.warning("renderer_disabled", reason="CRAWL_RENDER_URL not set")
            return None
        return HttpPageRenderer(
            settings.CRAWL_RENDER_URL,
            settings.CRAWL_RENDER_API_KEY,
            timeout=float(settings.CRAWL_FETCH_TIMEOUT_SECONDS) * 3,
        )

    raise ConfigError(f"unknown crawl render backend {backend!r}")
