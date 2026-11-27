"""Stage 6 CRM sync: one-way push of leads and their activity into a CRM."""

from app.services.growth.crm.service import CrmService

__all__ = ["CrmService"]
