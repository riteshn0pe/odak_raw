"""The real agent layer: definitions, tenant facts, render-at-invocation.

This package owns what an agent *is* — name, goal, description, system-prompt
template, tools, triggers, handover, approval and activation requirement — so
the three half-copies of that truth (``modelconfig.AGENT_KEYS``,
``graph.AGENT_DESCRIPTIONS``, the services' own module constants) all derive
from one place. See ``agent.md`` at the repo root for the plan this implements.
"""
