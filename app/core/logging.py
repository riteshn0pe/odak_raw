"""Structured logging. Tenant and request identifiers ride on every line.

Contextvars are used so identifiers set once in middleware appear on all downstream
logs without being threaded through call signatures.
"""

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
conversation_id_var: ContextVar[str | None] = ContextVar("conversation_id", default=None)
# Growth job attribution. Bound at the top of a growth job callback so every line
# it emits — including from services that know nothing about jobs — says which
# agent was running and under which job kind. Unset on request paths, and the
# injector drops unset vars, so ordinary API logs are unchanged.
agent_key_var: ContextVar[str | None] = ContextVar("agent_key", default=None)
job_kind_var: ContextVar[str | None] = ContextVar("job_kind", default=None)


def _inject_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, var in (
        ("request_id", request_id_var),
        ("tenant_id", tenant_id_var),
        ("conversation_id", conversation_id_var),
        ("agent_key", agent_key_var),
        ("job_kind", job_kind_var),
    ):
        value = var.get()
        if value is not None:
            event_dict[key] = value
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    # httpx logs every request line at INFO, URL with query string included —
    # and at least one vendor (IndiaMART) authenticates by query param, so that
    # line is a tenant credential in the log stream. Adapters log their own
    # call failures with only the path; the library line adds nothing but the
    # leak.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
