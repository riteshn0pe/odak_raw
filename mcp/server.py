

from __future__ import annotations

import os
import uuid

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="kaaaado-orchestrator",
    instructions=(
        "Kaaaado growth swarm — research, outreach, inbox triage, campaigns, "
        "store/CRM sync, reporting — via the orchestrator. Ask in natural "
        "language; the orchestrator reads live data and queues actions (report, "
        "research, campaign draft, insights refresh, approval decisions) behind "
        "the platform's gates. One question in, one grounded answer out. "
        "Permissions: tenant-isolated by API key; failures are per-tool errors "
        "never whole-turn crashes; user control is the revocable key and "
        "Telegram approve/reject."
    ),
)

BASE_URL = os.environ.get("KAAAADO_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("KAAAADO_API_KEY", "")
USE_A2A = os.environ.get("KAAAADO_USE_A2A", "0") == "1"


def _headers() -> dict[str, str]:
    if not API_KEY:
        raise RuntimeError("KAAAADO_API_KEY is not set — see scripts/seed.py output or POST /v1/keys")
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


async def _call_orchestrator(question: str) -> str:
    """Call the swarm's front door — A2A or REST — and return the answer text."""
    q = (question or "").strip()[:4000]
    if not q:
        return "No question provided."
    async with httpx.AsyncClient(timeout=60.0) as client:
        if USE_A2A:
            # A2A message/send envelope — synchronous Task
            payload = {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": q}],
                        "contextId": str(uuid.uuid4()),
                    }
                },
            }
            resp = await client.post(f"{BASE_URL}/a2a/message/send", json=payload, headers=_headers())
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                return f"A2A error {body['error']}: {body['error'].get('message','')}"
            result = body.get("result") or {}
            arts = result.get("artifacts") or []
            if arts and arts[0].get("parts"):
                return str(arts[0]["parts"][0].get("text") or "")
            return str(result)
        else:
            resp = await client.post(
                f"{BASE_URL}/v1/orchestrator/ask",
                json={"question": q},
                headers=_headers(),
            )
            resp.raise_for_status()
            return str(resp.json().get("answer") or "")


@mcp.tool()
async def orchestrator_ask(question: str) -> str:
    """Ask the orchestrator anything the dashboard can answer.

    Reads: leads, report, agent states, approvals, insights, campaign stats,
    message counts, profile. Actions: queue report/research/campaign/insight
    refresh, approve/reject. All data is tenant-isolated via the API key;
    queued actions still pass the jobs gate and approval gates — the tool
    reports what it queued, not what finished.
    """
    try:
        return await _call_orchestrator(question)
    except httpx.HTTPStatusError as exc:
        return f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
    except Exception as exc:
        return f"orchestrator_ask failed: {exc}"


@mcp.tool()
async def get_agent_card() -> str:
    """Fetch the public agent card (no auth) — capabilities, skills, endpoints."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{BASE_URL}/.well-known/agent-card.json")
        resp.raise_for_status()
        import json

        return json.dumps(resp.json(), indent=2)


@mcp.tool()
async def orchestrator_status() -> str:
    """Quick health + tenant check: card URL, USE_A2A mode, key prefix."""
    prefix = API_KEY[:12] + "…" if API_KEY else "(no key)"
    return f"base_url={BASE_URL} use_a2a={USE_A2A} key={prefix} — try get_agent_card or orchestrator_ask"


if __name__ == "__main__":
    # stdio is what Claude/OpenCode expect; SSE is available via `mcp.run(transport='sse')`
    mcp.run()
