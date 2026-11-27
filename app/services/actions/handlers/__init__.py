"""The platform's built-in action handlers — one module per handler.

Each module exposes `build(service) -> Handler`, taking the `ActionService` so a
handler can reach `service.notifier` and `service.repo`, and returning the
coroutine the dispatcher awaits as
`handler(action=..., arguments=..., ctx=...)`.

`FACTORIES` is keyed by `system_handler` value — not by action name. A tenant may
rename an action; the handler it resolves to is the platform's, and the closed set
of names is `SYSTEM_HANDLERS` in `app.domain.actions`.

Two of the modules here are not handlers: `base` holds the argument readers and
outcome constructors, and `contact_store` / `calendar_provider` hold the storage
and vendor plumbing that two handlers each would otherwise duplicate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.actions.handlers import (
    book_meeting,
    check_availability,
    collect_data,
    collect_lead,
    escalate_to_human,
    notify_slack,
    web_search,
)
from app.services.actions.handlers.base import Handler

HandlerFactory = Callable[[Any], Handler]

FACTORIES: dict[str, HandlerFactory] = {
    "escalate_to_human": escalate_to_human.build,
    "collect_lead": collect_lead.build,
    "collect_data": collect_data.build,
    "notify_slack": notify_slack.build,
    "web_search": web_search.build,
    "book_meeting": book_meeting.build,
    "check_availability": check_availability.build,
}

__all__ = ["FACTORIES", "Handler", "HandlerFactory"]
