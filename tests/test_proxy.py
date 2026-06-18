"""Tests for the proxy core logic.

We use a FakeAdapter and exercise the proxy's routing paths without
hitting any real APIs.
"""

import asyncio
from typing import Any, Dict, List

import pytest

from mcp_governance_proxy import (
    AllowListEvaluator,
    CallStatus,
    EvaluationResult,
    GovernanceProxy,
    NoOpEvaluator,
    ToolCall,
)
from tests.conftest import FakeAdapter


class HoldingEvaluator:
    """Always holds calls. Used to test the hold queue path."""
    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        return EvaluationResult(allow=False, hold=True, reason="manual hold for test")


class BlockingEvaluator:
    """Always blocks. Used to test the rejection path."""
    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        return EvaluationResult(allow=False, hold=False, reason="not allowed in test")


class BuggyEvaluator:
    """Raises an exception. Used to test the failure path."""
    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        raise RuntimeError("synthetic failure")


# ----- routing tests -----

@pytest.mark.asyncio
async def test_noop_evaluator_executes_call():
    adapter = FakeAdapter("test_tool")
    proxy = GovernanceProxy(evaluator=NoOpEvaluator(), adapters=[adapter])
    outcome = await proxy.handle_call(ToolCall(tool="test_tool", arguments={"x": 1}))
    assert outcome.status == CallStatus.EXECUTED
    assert outcome.result == {"echoed": {"x": 1}}
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_blocking_evaluator_rejects():
    adapter = FakeAdapter("test_tool")
    proxy = GovernanceProxy(evaluator=BlockingEvaluator(), adapters=[adapter])
    outcome = await proxy.handle_call(ToolCall(tool="test_tool", arguments={}))
    assert outcome.status == CallStatus.REJECTED
    assert "not allowed" in outcome.result
    assert adapter.calls == []  # never executed


@pytest.mark.asyncio
async def test_holding_evaluator_enqueues():
    adapter = FakeAdapter("test_tool")
    proxy = GovernanceProxy(evaluator=HoldingEvaluator(), adapters=[adapter])
    outcome = await proxy.handle_call(ToolCall(tool="test_tool", arguments={"x": 1}))
    assert outcome.status == CallStatus.HELD
    assert adapter.calls == []
    pending = proxy.hold_queue.list_pending()
    assert len(pending) == 1
    assert pending[0].call.tool == "test_tool"


@pytest.mark.asyncio
async def test_approve_held_executes():
    adapter = FakeAdapter("test_tool")
    proxy = GovernanceProxy(evaluator=HoldingEvaluator(), adapters=[adapter])
    outcome = await proxy.handle_call(ToolCall(tool="test_tool", arguments={"x": 42}))
    hold_id = outcome.result["hold_id"]

    # approve
    result = await proxy.approve_held(hold_id)
    assert result.status == CallStatus.EXECUTED
    assert result.result == {"echoed": {"x": 42}}
    # queue cleared
    assert proxy.hold_queue.list_pending() == []


@pytest.mark.asyncio
async def test_reject_held_does_not_execute():
    adapter = FakeAdapter("test_tool")
    proxy = GovernanceProxy(evaluator=HoldingEvaluator(), adapters=[adapter])
    outcome = await proxy.handle_call(ToolCall(tool="test_tool", arguments={"x": 1}))
    hold_id = outcome.result["hold_id"]

    ok = proxy.reject_held(hold_id)
    assert ok is True
    assert proxy.hold_queue.list_pending() == []
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_buggy_evaluator_returns_failure_not_crash():
    adapter = FakeAdapter("test_tool")
    proxy = GovernanceProxy(evaluator=BuggyEvaluator(), adapters=[adapter])
    outcome = await proxy.handle_call(ToolCall(tool="test_tool", arguments={}))
    assert outcome.status == CallStatus.FAILED
    assert "synthetic failure" in outcome.result


@pytest.mark.asyncio
async def test_unknown_tool_returns_failed():
    proxy = GovernanceProxy(evaluator=NoOpEvaluator(), adapters=[FakeAdapter("known")])
    outcome = await proxy.handle_call(ToolCall(tool="not_a_thing", arguments={}))
    assert outcome.status == CallStatus.FAILED
    assert "Unknown tool" in outcome.result


@pytest.mark.asyncio
async def test_allow_list_evaluator_allows_listed_blocks_rest():
    adapter1 = FakeAdapter("allowed_tool")
    adapter2 = FakeAdapter("blocked_tool")
    ev = AllowListEvaluator(allowed_tools=["allowed_tool"])
    proxy = GovernanceProxy(evaluator=ev, adapters=[adapter1, adapter2])

    out1 = await proxy.handle_call(ToolCall(tool="allowed_tool", arguments={}))
    out2 = await proxy.handle_call(ToolCall(tool="blocked_tool", arguments={}))
    assert out1.status == CallStatus.EXECUTED
    assert out2.status == CallStatus.REJECTED


@pytest.mark.asyncio
async def test_list_tools_returns_descriptors_from_all_adapters():
    proxy = GovernanceProxy(
        evaluator=NoOpEvaluator(),
        adapters=[FakeAdapter("tool_a"), FakeAdapter("tool_b")],
    )
    tools = proxy.list_tools()
    names = [t["name"] for t in tools]
    assert "tool_a" in names
    assert "tool_b" in names


@pytest.mark.asyncio
async def test_request_id_assigned_when_missing():
    proxy = GovernanceProxy(evaluator=NoOpEvaluator(), adapters=[FakeAdapter("t")])
    outcome = await proxy.handle_call(ToolCall(tool="t", arguments={}))
    assert outcome.request_id is not None
    assert outcome.request_id.startswith("req-")
