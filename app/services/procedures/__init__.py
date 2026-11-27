"""Procedure services: the template gallery, the validator, and the run machinery.

This package holds the procedure half of the agent platform — the shipped template
gallery (`library`), the save-time validator (`validate`), the authoring service
(`service`), and the run machinery that sibling modules add.

The initialiser performs no imports at module scope. Modules here are imported by
their full path, so adding a sibling never means editing this file, and importing
`library` — platform data with no dependencies — must not drag the service layer
and its database adapters in behind it. `ProcedureService` is still reachable as
`app.services.procedures.ProcedureService` for callers that prefer the package
name; the lookup below resolves it on first access rather than at import time, so
the convenience costs nothing at start-up.
"""

from typing import Any


def __getattr__(name: str) -> Any:
    """Resolve the package's one re-export lazily.

    PEP 562 module-level lookup, used here so the convenience name does not turn
    into an eager import of the service layer for every consumer of `library`.
    """
    if name == "ProcedureService":
        from app.services.procedures.service import ProcedureService

        return ProcedureService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
