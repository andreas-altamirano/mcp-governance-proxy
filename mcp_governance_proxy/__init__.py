"""MCP Governance Proxy.

An MCP server that sits between an MCP client (Claude Desktop, Cursor,
Cline, anything that speaks MCP) and the real systems the agent acts on.
Every tool call is evaluated by a pluggable policy engine before the proxy
calls the real API on the agent's behalf.

The agent never holds credentials. Risky actions hold for human review.
Everything is logged.

Quick start:

    from mcp_governance_proxy import GovernanceProxy, NoOpEvaluator, create_app
    from mcp_governance_proxy.adapters import SlackAdapter

    proxy = GovernanceProxy(
        evaluator=NoOpEvaluator(),     # swap for AllowListEvaluator or WaveEngineEvaluator
        adapters=[SlackAdapter()],
    )
    app = create_app(proxy)

Then run with: `uvicorn mcp_governance_proxy.examples.minimal_server:app --port 9000`

See examples/ for a working setup with wave-engine integration.
"""

from .evaluators import AllowListEvaluator, NoOpEvaluator, WaveEngineEvaluator
from .http_app import create_app
from .proxy import GovernanceProxy, HeldCall, InMemoryHoldQueue
from .types import (
    CallOutcome,
    CallStatus,
    EvaluationResult,
    Evaluator,
    ToolAdapter,
    ToolCall,
)

__version__ = "0.1.0"

__all__ = [
    "GovernanceProxy",
    "HeldCall",
    "InMemoryHoldQueue",
    "NoOpEvaluator",
    "AllowListEvaluator",
    "WaveEngineEvaluator",
    "create_app",
    "Evaluator",
    "ToolAdapter",
    "ToolCall",
    "EvaluationResult",
    "CallOutcome",
    "CallStatus",
]
