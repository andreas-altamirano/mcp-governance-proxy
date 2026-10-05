"""Minimal runnable MCP governance proxy.

This example wires together:
  - Wave engine evaluator with one policy (refunds over $100 hold for review)
  - A fake demo adapter (no real APIs called) so you can try the flow
  - The HTTP transport

Run:
    pip install -e ".[dev,wave]"
    MCP_PROXY_ADMIN_TOKEN=demo-token python examples/minimal_server.py

The /admin endpoints need that token (Authorization: Bearer demo-token).
Without MCP_PROXY_ADMIN_TOKEN they are disabled. Never give the token to an agent.

Then in another terminal:
    # List tools
    curl -X POST http://localhost:9000/mcp \\
      -H 'content-type: application/json' \\
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

    # Try a tool call that gets auto-approved
    curl -X POST http://localhost:9000/mcp \\
      -H 'content-type: application/json' \\
      -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_send_message","arguments":{"channel":"#general","text":"hi"}}}'

    # Try a tool call that gets held
    curl -X POST http://localhost:9000/mcp \\
      -H 'content-type: application/json' \\
      -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"demo_refund","arguments":{"amount":500}}}'

    # See what's pending review
    curl http://localhost:9000/admin/holds -H 'Authorization: Bearer demo-token'

    # Approve one
    curl -X POST http://localhost:9000/admin/holds/<hold_id>/approve \\
      -H 'Authorization: Bearer demo-token'
"""

import logging
from typing import Any, Dict, List

from wave_engine import Rule, Wave, WaveEvaluator, match_action, match_all, match_context_gt, match_system

from mcp_governance_proxy import GovernanceProxy, WaveEngineEvaluator, create_app
from mcp_governance_proxy.types import ToolCall


# -----------------------------
# A fake adapter so this demo runs without real API tokens.
# -----------------------------

class DemoAdapter:
    """No-op tools that "execute" by just echoing what they received."""

    @property
    def tool_names(self) -> List[str]:
        return ["demo_send_message", "demo_refund"]

    @property
    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        return {
            "demo_send_message": {
                "type": "object",
                "description": "(demo) Pretend to send a message.",
                "properties": {
                    "channel": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["channel", "text"],
            },
            "demo_refund": {
                "type": "object",
                "description": "(demo) Pretend to issue a refund.",
                "properties": {
                    "amount": {"type": "number"},
                    "destination": {"type": "string"},
                },
                "required": ["amount"],
            },
        }

    async def execute(self, call: ToolCall) -> Any:
        return {
            "demo": True,
            "tool": call.tool,
            "args": call.arguments,
            "note": "Would have called real API here.",
        }


# -----------------------------
# Build the wave-engine policy.
# -----------------------------

def build_policy() -> WaveEvaluator:
    return WaveEvaluator(rules=[
        # Big refunds hold for human review
        Rule(
            name="high_value_refund",
            matcher=match_all(
                match_system("demo"),
                match_action("refund"),
                match_context_gt("amount", 100),
            ),
            score=30,
            min_wave=Wave.REVIEW,
            reason="Refund over $100",
        ),
        # Routine messages just get logged
        Rule(
            name="message_send_baseline",
            matcher=match_action("send_message"),
            score=5,
            reason="Outbound message",
        ),
    ])


# -----------------------------
# Assemble the proxy and expose as an ASGI app.
# -----------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

proxy = GovernanceProxy(
    evaluator=WaveEngineEvaluator(build_policy()),
    adapters=[DemoAdapter()],
)
app = create_app(proxy)


if __name__ == "__main__":
    import uvicorn
    # Localhost only. Don't expose an unauthenticated-by-default demo to the network.
    uvicorn.run(app, host="127.0.0.1", port=9000)
