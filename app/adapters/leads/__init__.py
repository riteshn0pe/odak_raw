"""Paid lead-data and email-verification vendors.

One module per vendor, each implementing a port from ``app/domain/ports.py``.
Every adapter here is optional: the research agent's base case is web-only —
search, fetch, extract — and these raise the verified-contact rate rather than
enabling the feature. So each ``build_*`` returns ``None`` when unconfigured and
every method degrades to empty rather than raising, the same graceful-off
posture the reranker and web search already take.
"""
