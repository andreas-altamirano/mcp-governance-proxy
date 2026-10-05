"""HTTP transport for the proxy.

Exposes:
  POST /mcp                 - MCP JSON-RPC endpoint (tools/list, tools/call)
  GET  /health              - liveness check
  GET  /admin/holds         - list pending held calls
  POST /admin/holds/{id}/approve - approve held call
  POST /admin/holds/{id}/reject  - reject held call

The MCP endpoint speaks a minimal subset of MCP JSON-RPC sufficient for
Claude Desktop and similar clients. For full MCP streaming-transport support
swap this for the official `mcp` Python SDK.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .proxy import GovernanceProxy
from .types import CallStatus, ToolCall

logger = logging.getLogger("mcp_governance_proxy.http")


# ----- JSON-RPC envelopes -----

class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str
    params: Optional[Dict[str, Any]] = None


def _jsonrpc_ok(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _jsonrpc_error(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


# ----- App factory -----

ADMIN_TOKEN_ENV = "MCP_PROXY_ADMIN_TOKEN"


def create_app(proxy: GovernanceProxy, admin_token: Optional[str] = None) -> FastAPI:
    """Build a FastAPI app that exposes `proxy` over HTTP.

    SECURITY: the /admin/* endpoints approve and reject held calls. They are
    protected by a bearer token, supplied via `admin_token` or the
    MCP_PROXY_ADMIN_TOKEN environment variable. If no token is configured the
    admin API is DISABLED (503) rather than left open. Never give this token
    to the agent: anything that holds it can approve its own held calls.
    """
    token = admin_token if admin_token is not None else os.environ.get(ADMIN_TOKEN_ENV)
    if not token:
        logger.warning(
            "No admin token configured (set %s or pass admin_token). "
            "The /admin endpoints are disabled; held calls cannot be approved.",
            ADMIN_TOKEN_ENV,
        )

    def require_admin(request: Request) -> None:
        if not token:
            raise HTTPException(
                status_code=503,
                detail=f"Admin API disabled: no admin token configured ({ADMIN_TOKEN_ENV}).",
            )
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(
            supplied.strip().encode(), token.encode()
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing admin token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(
        title="MCP Governance Proxy",
        description="Sits between MCP clients and real systems. Evaluates every tool call.",
        version="0.1.0",
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "mcp-governance-proxy"}

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        try:
            body = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700, "Parse error: body is not valid JSON")
        if not isinstance(body, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request: expected a JSON object")
        try:
            rpc = JsonRpcRequest(**body)
        except Exception as e:
            return _jsonrpc_error(body.get("id"), -32700, f"Parse error: {e}")

        client_id = request.headers.get("x-client-id") or request.headers.get("x-mcp-session-id")

        if rpc.method == "initialize":
            return _jsonrpc_ok(rpc.id, {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "mcp-governance-proxy", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            })

        if rpc.method == "tools/list":
            return _jsonrpc_ok(rpc.id, {"tools": proxy.list_tools()})

        if rpc.method == "tools/call":
            params = rpc.params or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if not name:
                return _jsonrpc_error(rpc.id, -32602, "Missing 'name' parameter")

            call = ToolCall(tool=name, arguments=args, client_id=client_id, request_id=str(rpc.id))
            outcome = await proxy.handle_call(call)

            # MCP expects a `content` array of message parts.
            content = [{
                "type": "text",
                "text": _format_outcome_for_client(outcome),
            }]
            return _jsonrpc_ok(rpc.id, {
                "content": content,
                "isError": outcome.status in (CallStatus.REJECTED, CallStatus.FAILED),
            })

        return _jsonrpc_error(rpc.id, -32601, f"Unknown method: {rpc.method}")

    # ----- Admin endpoints (for review UI) -----

    @app.get("/admin/holds", dependencies=[Depends(require_admin)])
    async def list_holds():
        return {
            "holds": [
                {
                    "id": h.id,
                    "tool": h.call.tool,
                    "arguments": h.call.arguments,
                    "client_id": h.call.client_id,
                    "reason": h.evaluation.reason,
                    "metadata": h.evaluation.metadata,
                }
                for h in proxy.hold_queue.list_pending()
            ]
        }

    @app.post("/admin/holds/{hold_id}/approve", dependencies=[Depends(require_admin)])
    async def approve_hold(hold_id: str):
        outcome = await proxy.approve_held(hold_id)
        if outcome.status == CallStatus.FAILED:
            raise HTTPException(status_code=404, detail=outcome.result)
        return outcome.to_dict()

    @app.post("/admin/holds/{hold_id}/reject", dependencies=[Depends(require_admin)])
    async def reject_hold(hold_id: str):
        ok = proxy.reject_held(hold_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"No held call with id {hold_id}")
        return {"status": "rejected", "hold_id": hold_id}

    return app


def _format_outcome_for_client(outcome) -> str:
    """Turn a CallOutcome into text the MCP client can show to the user."""
    if outcome.status == CallStatus.EXECUTED:
        return f"Executed. Result: {outcome.result}"
    if outcome.status == CallStatus.HELD:
        return (
            f"Held for human review.\n"
            f"Reason: {outcome.evaluation.reason}\n"
            f"Approval ID: {outcome.result.get('hold_id') if isinstance(outcome.result, dict) else 'unknown'}"
        )
    if outcome.status == CallStatus.REJECTED:
        return f"Rejected by policy: {outcome.evaluation.reason}"
    return f"Failed: {outcome.result}"
