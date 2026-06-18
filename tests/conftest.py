"""Shared test fixtures and helpers."""

from typing import Any, Dict, List

from mcp_governance_proxy import ToolCall


class FakeAdapter:
    """Always-succeeds adapter that records what it was called with."""

    def __init__(self, name: str = "test_tool") -> None:
        self._name = name
        self.calls: List[ToolCall] = []

    @property
    def tool_names(self) -> List[str]:
        return [self._name]

    @property
    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        return {self._name: {"type": "object", "description": "Fake tool for testing."}}

    async def execute(self, call: ToolCall) -> Any:
        self.calls.append(call)
        return {"echoed": call.arguments}
