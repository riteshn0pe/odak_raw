"""Security posture: what we store, where, who runs it, and for how long.

One read, served from reviewed constants and the deployment's own configuration.
It exists because an IT reviewer gets sent a URL and has to fill in a
questionnaire from it, and because the alternative — the dashboard hardcoding
residency strings — makes an incorrect residency claim a frontend bug. A wrong
answer here is contractual liability, so the rules are:

  * A region is reported only when the deployment actually declares one. There is
    no default and no guess. Unconfigured returns `null` plus the disclosure the
    UI must render instead: "Ask us where your data is stored — we'll tell you in
    writing."
  * Providers are derived from the configured backends rather than written down,
    so switching object storage cannot leave a stale provider name in the answer.
  * Retention windows are reviewed constants, not settings. They are commitments
    that should change through code review, which is also why `LAST_REVIEWED`
    ships alongside them and is rendered next to every claim.

Scope note: this module is only `GET /v1/security/posture`. The `/security`
screen's other cards belong to the dashboard stage that owns them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import ContainerDep, TenantDep

router = APIRouter(prefix="/security", tags=["security"])

# The date the claims below were last read and signed off. Rendered beside them.
LAST_REVIEWED = "2026-08-12"

# What the UI shows in place of a region when the deployment declares none. Never
# substitute a plausible-looking region: an invented residency claim is worse than
# an admitted gap.
REGION_DISCLOSURE = "Ask us where your data is stored — we'll tell you in writing."

# Scoped deliberately. The unqualified claim is not true across provider tiers: a
# tenant who connects their own provider account is bound by that provider's terms.
MODEL_TRAINING_NOTE = (
    "Your questions and documents are not used to train the models we run for you. "
    "If you connect your own provider account, that provider's terms apply."
)

# §7.6 Table 2. Windows, not prose: each one is a commitment with a mechanism
# behind it, and the "why" is the sentence the tenant reads.
RETENTION: tuple[tuple[str, str, str], ...] = (
    (
        "Conversations and messages",
        "400 days",
        "So you can review a full year plus a comparison period.",
    ),
    ("Uploaded files", "Until you delete them", "They're yours."),
    (
        "Passages used for search",
        "As long as the source exists",
        "Deleting a source removes them from search immediately.",
    ),
    ("Handoff records", "400 days", "Matches conversations."),
    ("Email receipts", "90 days", "Delivery troubleshooting only."),
    ("Audit log", "Life of the workspace", "It's the record of who changed what."),
    (
        "Raw incoming email",
        "Not stored",
        "We read it, act on it, and don't keep the original message.",
    ),
    (
        "Deleted sources",
        "Out of search immediately; files removed within 30 days",
        "Backups may hold a copy until they age out.",
    ),
)

# Isolation and access controls, each paired with the mechanism that implements it.
# A claim with no mechanism does not belong in this response.
CONTROLS: tuple[tuple[str, bool, str], ...] = (
    (
        "Database separation",
        True,
        "Every query is limited to your workspace by the database itself "
        "(PostgreSQL row-level security, enforced).",
    ),
    (
        "Search separation",
        True,
        "Passage search is filtered to your workspace before it runs.",
    ),
    (
        "File separation",
        True,
        "Your files sit under a path only your workspace can read.",
    ),
    (
        "Document links",
        True,
        "Links to your files expire after a few minutes and are signed.",
    ),
    (
        "Personal details masked before the model",
        True,
        "Emails, phone numbers and card-like numbers are redacted on the way out.",
    ),
    (
        "Change log that cannot be edited",
        True,
        "Every change to an agent or a key is appended to an audit log with no "
        "update or delete path.",
    ),
    (
        "Single sign-on (SSO)",
        False,
        "Not available yet. Access is email and password with per-workspace roles.",
    ),
)

# Backend identifier -> the operator's name as a reviewer would recognise it.
_PROVIDERS: dict[str, str] = {
    "supabase": "Supabase",
    "local": "Self-hosted (this deployment's own filesystem)",
    "qdrant": "Qdrant",
    "memory": "Self-hosted (in-process, not for production)",
    "gemini": "Google",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "nvidia": "NVIDIA",
    "jina": "Jina AI",
    "cohere": "Cohere",
    "fake": "Self-hosted (test embedder, not for production)",
}


def _provider(backend: str) -> str:
    return _PROVIDERS.get(backend, backend)


class PostureStore(BaseModel):
    what: str
    where: str
    provider: str
    # null when this deployment declares no region. The client must render the
    # disclosure rather than fill the gap.
    region: str | None


class PostureRetention(BaseModel):
    what: str
    kept_for: str
    why: str


class PostureControl(BaseModel):
    name: str
    enabled: bool
    detail: str


class SecurityPostureOut(BaseModel):
    last_reviewed: str
    # false means every `region` below is null, and the UI shows `region_disclosure`
    regions_configured: bool
    region_disclosure: str
    model_training_note: str
    stores: list[PostureStore]
    retention: list[PostureRetention]
    controls: list[PostureControl]


@router.get("/posture", response_model=SecurityPostureOut)
async def security_posture(tenant: TenantDep, container: ContainerDep) -> SecurityPostureOut:
    """Where this workspace's data lives, who operates it, and how long it is kept.

    Authenticated because it describes the deployment, not the product: an
    unauthenticated version would let anyone enumerate which providers a customer
    runs on. Cheap enough to be uncached — it reads settings and constants and
    touches neither the database nor a provider.
    """
    settings: Any = container.settings

    database_provider = "Supabase" if settings.SUPABASE_URL else "Self-hosted PostgreSQL"
    # No region setting exists in this deployment's configuration, so there is
    # nothing to report and nothing is invented. When residency is declared, it is
    # declared once here and every row picks it up.
    region: str | None = None

    stores = [
        PostureStore(
            what="Uploaded files and crawled pages",
            where="Object storage",
            provider=_provider(settings.STORAGE_BACKEND),
            region=region,
        ),
        PostureStore(
            what="Passages used for search",
            where="Vector database",
            provider=_provider(settings.VECTOR_BACKEND),
            region=region,
        ),
        PostureStore(
            what="Conversations, agents, settings",
            where="PostgreSQL",
            provider=database_provider,
            region=region,
        ),
        PostureStore(
            what="Backups",
            where="PostgreSQL snapshots",
            provider=database_provider,
            region=region,
        ),
        PostureStore(
            what="Questions sent to the model",
            where="Not stored by the model provider",
            provider=_provider(settings.LLM_BACKEND),
            region=region,
        ),
        PostureStore(
            what="Text turned into search vectors",
            where="Not stored by the embedding provider",
            provider=_provider(settings.EMBEDDING_BACKEND),
            region=region,
        ),
    ]

    return SecurityPostureOut(
        last_reviewed=LAST_REVIEWED,
        regions_configured=region is not None,
        region_disclosure=REGION_DISCLOSURE,
        model_training_note=MODEL_TRAINING_NOTE,
        stores=stores,
        retention=[
            PostureRetention(what=what, kept_for=kept_for, why=why)
            for what, kept_for, why in RETENTION
        ],
        controls=[
            PostureControl(name=name, enabled=enabled, detail=detail)
            for name, enabled, detail in CONTROLS
        ],
    )
