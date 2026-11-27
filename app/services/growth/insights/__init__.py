"""Part B insights agent: watchers find candidates, the service decides."""

from app.services.growth.insights.service import InsightsService
from app.services.growth.insights.watchers import Candidate, make_uid

__all__ = ["Candidate", "InsightsService", "make_uid"]
