# mcp-governance-proxy

**An MCP server that sits between AI agents and the systems they act on. Evaluates every tool call before it executes. Holds risky ones for human review.**

Built for teams running AI agents (Claude Desktop, Cursor, Cline, in-house) that need to govern what those agents can actually do on real systems — without rewriting every agent to add policy checks.

```text
┌────────────┐        ┌──────────────────────┐        ┌──────────────┐
│ MCP Client │        │ mcp-governance-proxy │        │ Real Systems │
│ (Claude    │  MCP   │                      │  API   │ Slack        │
│  Desktop,  │ ─────► │  1. evaluate         │ ─────► │ GitHub       │
│  Cursor,   │        │  2. auto/hold/block  │        │ AWS          │
│  Cline)    │        │  3. log every action │        │ ...          │
└────────────┘        └──────────────────────┘        └──────────────┘
                              ▲
                              │ credentials live here
                              │ (never in the agent)
```

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

---

## Why this exists

Today's AI agents call tools — Slack, GitHub, Stripe, internal APIs. Two real problems:

1. **The agent holds the credentials.** If the agent is compromised, or the model is jailbroken, or it just makes a mistake, the credentials are weaponized. Most production agent setups treat OAuth tokens as if they're throwaway.

2. **There's no consistent place to evaluate "should this happen?"** Policy lives scattered in agent code, infra rules, vendor-specific consoles, or nowhere at all. Adding governance means rewriting every agent.

This proxy solves both. The agent talks MCP to the proxy. The proxy holds the credentials, evaluates every call through a pluggable policy engine, executes if allowed, holds for review if risky, blocks if disallowed. The agent never sees a real API token.

---

## Install

```bash
pip install mcp-governance-proxy                # core
pip install "mcp-governance-proxy[wave]"        # + wave-engine evaluator integration
```

Python 3.9+. Depends on `fastapi`, `httpx`, `pydantic`, `uvicorn`.

---

## Quick start

```python
from mcp_governance_proxy import GovernanceProxy, AllowListEvaluator, create_app
from mcp_governance_proxy.adapters import SlackAdapter, GitHubAdapter

# 1. Build the proxy: evaluator + adapters
proxy = GovernanceProxy(
    evaluator=AllowListEvaluator(allowed_tools=[
        "slack_send_message",
        "github_create_issue",
    ]),
    adapters=[
        SlackAdapter(),    # reads SLACK_BOT_TOKEN from env
        GitHubAdapter(),   # reads GITHUB_TOKEN from env
    ],
)

# 2. Wrap in a FastAPI app
app = create_app(proxy)

# 3. Run
#    uvicorn yourmodule:app --port 9000
```

Point any MCP client at `http://localhost:9000/mcp` and it can now use `slack_send_message` and `github_create_issue` — but only those, and only after the proxy evaluates each call.

---

## Plug in wave-engine for nuanced policy

`AllowListEvaluator` is binary. For continuous 1–5 risk scoring (auto-execute the safe, hold the risky for review, block the worst), pair the proxy with [wave-engine](https://github.com/andreas-altamirano/wave-engine):

```python
from wave_engine import (
    Rule, Wave, WaveEvaluator,
    match_all, match_action, match_context_gt, match_system,
)
from mcp_governance_proxy import GovernanceProxy, WaveEngineEvaluator, create_app
from mcp_governance_proxy.adapters import SlackAdapter

policy = WaveEvaluator(rules=[
    Rule(
        name="high_value_refund",
        matcher=match_all(
            match_system("stripe"),
            match_action("refund"),
            match_context_gt("amount", 100),
        ),
        score=30,
        min_wave=Wave.REVIEW,   # force hold-for-human
        reason="Refund over $100",
    ),
])

proxy = GovernanceProxy(
    evaluator=WaveEngineEvaluator(policy),
    adapters=[SlackAdapter()],
)
app = create_app(proxy)
```

Now refunds under $100 auto-execute, refunds over $100 hold for human approval at `/admin/holds`, and everything is logged with a Wave score.

---

## Try the demo locally

```bash
git clone https://github.com/andreas-altamirano/mcp-governance-proxy
cd mcp-governance-proxy
pip install -e ".[dev]"
python examples/minimal_server.py
```

Server starts on `http://localhost:9000`. In another terminal:

```bash
# List available tools
curl -X POST http://localhost:9000/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Tool call that auto-executes
curl -X POST http://localhost:9000/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"demo_send_message",
                 "arguments":{"channel":"#general","text":"hi"}}}'

# Tool call that gets held for review
curl -X POST http://localhost:9000/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"demo_refund","arguments":{"amount":500}}}'

# See what's pending review
curl http://localhost:9000/admin/holds

# Approve a held call
curl -X POST http://localhost:9000/admin/holds/<hold_id>/approve
```

The demo runs without any real API tokens — it uses a fake adapter that just echoes what it received.

---

## API surface

### HTTP endpoints (from `create_app`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/mcp` | POST | MCP JSON-RPC: `initialize`, `tools/list`, `tools/call` |
| `/health` | GET | Liveness check |
| `/admin/holds` | GET | List held calls awaiting review |
| `/admin/holds/{id}/approve` | POST | Approve a held call (executes it now) |
| `/admin/holds/{id}/reject` | POST | Reject a held call (drops it) |

### Python API

```python
GovernanceProxy(evaluator, adapters, hold_queue=InMemoryHoldQueue())

# Async entry point
await proxy.handle_call(ToolCall(tool, arguments, client_id=None)) -> CallOutcome

# Review surface
await proxy.approve_held(hold_id) -> CallOutcome
proxy.reject_held(hold_id) -> bool
proxy.hold_queue.list_pending() -> list[HeldCall]
```

---

## Plug in your own evaluator

Implement the `Evaluator` protocol:

```python
from mcp_governance_proxy import EvaluationResult, ToolCall

class MyEvaluator:
    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        if call.tool == "send_money" and call.arguments.get("amount", 0) > 10000:
            return EvaluationResult(allow=False, hold=True,
                                    reason="Large transfer needs review")
        return EvaluationResult(allow=True, reason="OK")

proxy = GovernanceProxy(evaluator=MyEvaluator(), adapters=[...])
```

That's the whole interface. Evaluators can call out to OPA, query an external policy service, run an LLM classifier, or anything else — the proxy doesn't care.

---

## Plug in your own tool adapter

Implement the `ToolAdapter` protocol:

```python
class JiraAdapter:
    @property
    def tool_names(self):
        return ["jira_create_ticket"]

    @property
    def tool_schemas(self):
        return {
            "jira_create_ticket": {
                "type": "object",
                "description": "Create a Jira ticket.",
                "properties": {
                    "project": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["project", "summary"],
            }
        }

    async def execute(self, call):
        # call your Jira API, return the result
        ...
```

Reference adapters live in `mcp_governance_proxy/adapters.py` — `SlackAdapter`, `GitHubAdapter`, and a generic `WebhookAdapter`. Copy and adapt for your systems.

---

## What this is *not*

- **Not a policy authoring tool.** Write policy in `wave-engine`, OPA, Cedar, plain Python, or whatever else. This is the runtime that enforces it.
- **Not a full MCP SDK.** It speaks enough of MCP JSON-RPC for Claude Desktop and similar clients. For full streamable-transport / stdio MCP support, swap the HTTP layer for the official [mcp-python](https://github.com/modelcontextprotocol/python-sdk) SDK.
- **Not a credential vault.** Credentials live in env vars or whatever your existing secret store provides. The proxy reads them when it needs them.
- **Not a UI.** The `/admin/holds` endpoint returns JSON. Build whatever review UI fits your team on top of it.

---

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

---

## License

Apache 2.0. See [LICENSE](LICENSE).

---

## Acknowledgments

Extracted and generalized from the MCP governance layer of **Surfit**, an AI agent governance platform. Released so other teams can build governance into their agent stacks without committing to a full platform.

Companion libraries:

- [`wave-engine`](https://github.com/andreas-altamirano/wave-engine) — continuous 1–5 risk scoring (pairs with `WaveEngineEvaluator`)
- [`cross-system-correlator`](https://github.com/andreas-altamirano/cross-system-correlator) — pattern detection across multiple agent actions over time
