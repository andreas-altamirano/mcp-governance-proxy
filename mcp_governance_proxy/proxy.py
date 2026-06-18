"""The core proxy class.

A GovernanceProxy ties together evaluators, tool adapters, and a hold queue.
It exposes a single async method `handle_call(call)` that returns a CallOutcome.

The transport layer (MCP server, REST, gRPC, whatever) calls into this.
The proxy itself is transport-agnostic.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .types import (
    CallOutcome,
    CallStatus,
    EvaluationResult,
    Evaluator,
    HeldCallHandler,
    ToolAdapter,
    ToolCall,
)

logger = logging.getLogger("mcp_governance_proxy")


@dataclass
class HeldCall:
    """A call that was held for human review, waiting for a decision."""
    call: ToolCall
    evaluation: EvaluationResult
    id: str


class InMemoryHoldQueue:
    """Default hold-queue: keeps held calls in memory.

    For production, replace with a persistent backend (DB, Redis, etc) by
    implementing the same `enqueue`, `list_pending`, `approve`, `reject` API.
    """

    def __init__(self) -> None:
        self._holds: Dict[str, HeldCall] = {}

    def enqueue(self, call: ToolCall, evaluation: EvaluationResult) -> str:
        hold_id = f"hold-{uuid.uuid4().hex[:12]}"
        self._holds[hold_id] = HeldCall(call=call, evaluation=evaluation, id=hold_id)
        return hold_id

    def list_pending(self) -> List[HeldCall]:
        return list(self._holds.values())

    def get(self, hold_id: str) -> Optional[HeldCall]:
        return self._holds.get(hold_id)

    def remove(self, hold_id: str) -> Optional[HeldCall]:
        return self._holds.pop(hold_id, None)


@dataclass
class GovernanceProxy:
    """The proxy. Pass it an evaluator and a list of tool adapters."""
    evaluator: Evaluator
    adapters: List[ToolAdapter] = field(default_factory=list)
    hold_queue: InMemoryHoldQueue = field(default_factory=InMemoryHoldQueue)

    # ----- adapter registry -----

    def _find_adapter(self, tool_name: str) -> Optional[ToolAdapter]:
        for adapter in self.adapters:
            if tool_name in adapter.tool_names:
                return adapter
        return None

    def list_tools(self) -> List[Dict]:
        """Return MCP tool descriptors for all registered tools."""
        tools = []
        for adapter in self.adapters:
            for name in adapter.tool_names:
                schema = adapter.tool_schemas.get(name, {"type": "object"})
                tools.append({
                    "name": name,
                    "description": schema.get("description", ""),
                    "inputSchema": schema,
                })
        return tools

    # ----- the main entry point -----

    async def handle_call(self, call: ToolCall) -> CallOutcome:
        """Evaluate and route a tool call. Always returns a CallOutcome."""
        if call.request_id is None:
            call.request_id = f"req-{uuid.uuid4().hex[:12]}"

        # 1. Resolve adapter (fail fast if tool is unknown)
        adapter = self._find_adapter(call.tool)
        if adapter is None:
            logger.warning("Unknown tool '%s' from client %s", call.tool, call.client_id)
            return CallOutcome(
                status=CallStatus.FAILED,
                result=f"Unknown tool: {call.tool}",
                evaluation=EvaluationResult(allow=False, reason="Unknown tool"),
                request_id=call.request_id,
            )

        # 2. Evaluate
        try:
            evaluation = await self.evaluator.evaluate(call)
        except Exception as e:
            logger.exception("Evaluator raised on call %s", call.request_id)
            return CallOutcome(
                status=CallStatus.FAILED,
                result=f"Evaluator error: {e}",
                evaluation=EvaluationResult(allow=False, reason=str(e)),
                request_id=call.request_id,
            )

        # 3. Route based on evaluation
        if evaluation.rejected:
            logger.info("REJECTED %s: %s", call.tool, evaluation.reason)
            return CallOutcome(
                status=CallStatus.REJECTED,
                result=evaluation.reason or "Call rejected by policy.",
                evaluation=evaluation,
                request_id=call.request_id,
            )

        if evaluation.hold:
            hold_id = self.hold_queue.enqueue(call, evaluation)
            logger.info("HELD %s for review: %s [hold_id=%s]",
                        call.tool, evaluation.reason, hold_id)
            return CallOutcome(
                status=CallStatus.HELD,
                result={
                    "message": "Call held for human review.",
                    "hold_id": hold_id,
                    "reason": evaluation.reason,
                },
                evaluation=evaluation,
                request_id=call.request_id,
            )

        # 4. Allow → execute
        try:
            result = await adapter.execute(call)
        except Exception as e:
            logger.exception("Adapter execution failed for %s", call.tool)
            return CallOutcome(
                status=CallStatus.FAILED,
                result=f"Execution error: {e}",
                evaluation=evaluation,
                request_id=call.request_id,
            )

        logger.info("EXECUTED %s", call.tool)
        return CallOutcome(
            status=CallStatus.EXECUTED,
            result=result,
            evaluation=evaluation,
            request_id=call.request_id,
        )

    # ----- approval API (for the held-call review surface) -----

    async def approve_held(self, hold_id: str) -> CallOutcome:
        """Approve a held call. Executes it now."""
        held = self.hold_queue.remove(hold_id)
        if held is None:
            return CallOutcome(
                status=CallStatus.FAILED,
                result=f"No held call with id {hold_id}",
                evaluation=EvaluationResult(allow=False, reason="hold_id not found"),
            )
        adapter = self._find_adapter(held.call.tool)
        if adapter is None:
            return CallOutcome(
                status=CallStatus.FAILED,
                result=f"Unknown tool: {held.call.tool}",
                evaluation=held.evaluation,
                request_id=held.call.request_id,
            )
        try:
            result = await adapter.execute(held.call)
        except Exception as e:
            return CallOutcome(
                status=CallStatus.FAILED,
                result=f"Execution error: {e}",
                evaluation=held.evaluation,
                request_id=held.call.request_id,
            )
        return CallOutcome(
            status=CallStatus.EXECUTED,
            result=result,
            evaluation=held.evaluation,
            request_id=held.call.request_id,
        )

    def reject_held(self, hold_id: str, reason: str = "") -> bool:
        """Reject a held call without executing. Returns True if found."""
        return self.hold_queue.remove(hold_id) is not None
