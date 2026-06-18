"""Integration tests for the WaveEngineEvaluator wrapper.

These tests require wave-engine to be installed (it's in the optional
[wave] extra). They prove the two libraries compose correctly.
"""

import pytest

pytest.importorskip("wave_engine")

from wave_engine import (  # noqa: E402
    Rule,
    Wave,
    WaveEvaluator,
    match_action,
    match_all,
    match_context_gt,
    match_system,
)

from mcp_governance_proxy import (  # noqa: E402
    CallStatus,
    GovernanceProxy,
    ToolCall,
    WaveEngineEvaluator,
)
from tests.conftest import FakeAdapter  # noqa: E402


def _build_wave():
    return WaveEvaluator(rules=[
        Rule(
            name="high_value_refund",
            matcher=match_all(
                match_system("stripe"),
                match_action("refund"),
                match_context_gt("amount", 100),
            ),
            score=30,
            min_wave=Wave.REVIEW,
            reason="Refund over $100",
        ),
        Rule(
            name="small_action_ok",
            matcher=match_system("slack"),
            score=5,
            reason="slack message",
        ),
    ])


@pytest.mark.asyncio
async def test_wave_high_value_refund_holds():
    wave_ev = WaveEngineEvaluator(_build_wave())
    proxy = GovernanceProxy(
        evaluator=wave_ev,
        adapters=[FakeAdapter("stripe_refund")],
    )
    outcome = await proxy.handle_call(ToolCall(
        tool="stripe_refund",
        arguments={"amount": 500, "destination": "cus_abc"},
        client_id="agent-1",
    ))
    assert outcome.status == CallStatus.HELD
    assert outcome.evaluation.metadata["wave"] == 4
    assert "Refund over $100" in outcome.evaluation.reason


@pytest.mark.asyncio
async def test_wave_small_action_auto_executes():
    wave_ev = WaveEngineEvaluator(_build_wave())
    proxy = GovernanceProxy(
        evaluator=wave_ev,
        adapters=[FakeAdapter("slack_send_message")],
    )
    outcome = await proxy.handle_call(ToolCall(
        tool="slack_send_message",
        arguments={"channel": "#general", "text": "hi"},
    ))
    assert outcome.status == CallStatus.EXECUTED
    assert outcome.evaluation.metadata["wave"] in (1, 2)
