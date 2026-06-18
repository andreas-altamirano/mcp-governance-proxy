"""Core types for the MCP governance proxy.

A ToolCall is what an MCP client sends. The proxy evaluates it, optionally
holds it for review, and (if approved) hands it to a ToolAdapter that calls
the real system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol


class CallStatus(str, Enum):
    EXECUTED = "executed"
    HELD = "held"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class ToolCall:
    """An incoming tool call from an MCP client.

    Fields:
        tool: name of the tool being invoked (e.g. "slack_send_message").
        arguments: arguments the client passed (the tool's input schema).
        client_id: optional client identifier (the MCP session, agent name,
                  or whatever the transport surfaces).
        request_id: optional unique id for this call (for tracing).
    """
    tool: str
    arguments: Dict[str, Any]
    client_id: Optional[str] = None
    request_id: Optional[str] = None


@dataclass
class EvaluationResult:
    """An evaluator's verdict on a ToolCall.

    Fields:
        allow: True if the call should be forwarded to the adapter.
        hold: True if the call should be held for human review instead.
        reason: short human-readable explanation.
        metadata: free-form dict the evaluator may attach (wave number,
                 risk score, matched rules, etc) for downstream logging.
    """
    allow: bool
    hold: bool = False
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return not self.allow and not self.hold


@dataclass
class CallOutcome:
    """The final outcome the proxy returns to the client.

    Fields:
        status: EXECUTED, HELD, REJECTED, or FAILED.
        result: tool result if executed; description if held/rejected/failed.
        evaluation: the EvaluationResult that led here.
        request_id: same as ToolCall.request_id if provided.
    """
    status: CallStatus
    result: Any
    evaluation: EvaluationResult
    request_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "result": self.result,
            "evaluation": {
                "allow": self.evaluation.allow,
                "hold": self.evaluation.hold,
                "reason": self.evaluation.reason,
                "metadata": self.evaluation.metadata,
            },
            "request_id": self.request_id,
        }


class Evaluator(Protocol):
    """Plug-in interface for action evaluators.

    Implement this to plug your own policy engine into the proxy.
    The proxy ships with a NoOp evaluator (allow everything, useful for
    testing) and a WaveEngineEvaluator wrapper (delegates to wave-engine).
    """

    async def evaluate(self, call: ToolCall) -> EvaluationResult: ...


class ToolAdapter(Protocol):
    """Plug-in interface for tool adapters.

    A tool adapter knows how to actually execute one or more tools against
    a real system. The proxy ships with reference adapters for Slack,
    GitHub, and a generic webhook adapter as examples.
    """

    @property
    def tool_names(self) -> list: ...
    """Tool names this adapter handles, e.g. ["slack_send_message"]."""

    @property
    def tool_schemas(self) -> Dict[str, Dict[str, Any]]: ...
    """MCP tool input schemas keyed by tool name."""

    async def execute(self, call: ToolCall) -> Any: ...
    """Execute the tool call against the real system, return the result."""


# Type alias for held-call handlers — user-defined async function that
# decides whether/when to release a held call. Default just stores it
# in memory for a separate approval UI to pick up.
HeldCallHandler = Callable[[ToolCall, EvaluationResult], Awaitable[CallOutcome]]
