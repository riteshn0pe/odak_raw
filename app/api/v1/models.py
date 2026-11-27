"""Model metadata for the dashboard's agent model settings.

One read that tells the frontend what a tenant can generate with: the managed
platform default. The dashboard's agent model settings are built from this shape.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import ContainerDep, TenantDep

router = APIRouter(prefix="/models", tags=["models"])


class ManagedModelOut(BaseModel):
    backend: str
    model: str
    label: str


class ModelsOut(BaseModel):
    managed: ManagedModelOut


@router.get("", response_model=ModelsOut)
async def list_models(tenant: TenantDep, container: ContainerDep) -> ModelsOut:
    """The managed platform default."""
    settings = container.settings
    managed = ManagedModelOut(
        backend=settings.LLM_BACKEND,
        model=settings.DEFAULT_LLM_MODEL,
        label="Managed (default)",
    )
    return ModelsOut(managed=managed)
