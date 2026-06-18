"""Evaluators shipped with the proxy.

- NoOpEvaluator: allow everything. Useful for testing.
- AllowListEvaluator: allow only listed tools. Simplest real policy.
- WaveEngineEvaluator: delegate to a wave-engine WaveEvaluator instance.

You can plug in your own by implementing the Evaluator protocol from types.py.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .types import EvaluationResult, ToolCall


class NoOpEvaluator:
    """Approves every call. Use for testing only — production should plug in real policy."""

    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        return EvaluationResult(allow=True, reason="NoOp evaluator (allow all)")


class AllowListEvaluator:
    """Allows calls whose tool name is in the allowlist; rejects everything else."""

    def __init__(self, allowed_tools: List[str]) -> None:
        self.allowed = set(allowed_tools)

    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        if call.tool in self.allowed:
            return EvaluationResult(allow=True, reason="Tool in allowlist")
        return EvaluationResult(allow=False, reason=f"Tool '{call.tool}' not in allowlist")


class WaveEngineEvaluator:
    """Wrap a wave-engine WaveEvaluator instance.

    The wrapper converts ToolCall → wave_engine.Action, evaluates, and
    converts the Wave Decision → EvaluationResult.

    Mapping:
        Outcome.AUTO    → allow=True, hold=False
        Outcome.REVIEW  → allow=False, hold=True
        Outcome.BLOCK   → allow=False, hold=False (rejected)

    You provide an `action_mapper` to control how a ToolCall becomes an
    Action. By default the proxy uses the tool name as both system and action.
    """

    def __init__(
        self,
        wave_evaluator: Any,  # wave_engine.WaveEvaluator
        action_mapper: Optional[Callable[[ToolCall], Any]] = None,
    ) -> None:
        try:
            import wave_engine  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "WaveEngineEvaluator requires wave-engine. Install with: pip install wave-engine"
            ) from e
        self.wave = wave_evaluator
        self._action_mapper = action_mapper or self._default_action_mapper

    @staticmethod
    def _default_action_mapper(call: ToolCall) -> Any:
        """Default mapping: tool name → (system, action), arguments → context."""
        from wave_engine import Action
        # Convention: tool names look like "slack_send_message" → system="slack", action="send_message"
        parts = call.tool.split("_", 1)
        system = parts[0] if parts else call.tool
        action = parts[1] if len(parts) > 1 else "call"
        # Pick destination from common argument names; else None.
        args = call.arguments or {}
        destination = (
            args.get("recipient")
            or args.get("to")
            or args.get("channel")
            or args.get("destination")
            or args.get("repo")
            or args.get("url")
        )
        return Action(
            system=system,
            action=action,
            destination=destination,
            context={k: v for k, v in args.items() if k not in {"recipient", "to", "channel", "destination"}},
            actor_id=call.client_id,
        )

    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        action = self._action_mapper(call)
        decision = self.wave.evaluate(action)

        metadata: Dict[str, Any] = {
            "wave": int(decision.wave),
            "wave_label": decision.wave.label,
            "outcome": decision.outcome,
            "score": decision.score,
            "matched_rules": list(decision.matched_rules),
        }

        if decision.outcome == "auto":
            return EvaluationResult(allow=True, hold=False, reason=decision.reasoning, metadata=metadata)
        if decision.outcome == "review":
            return EvaluationResult(allow=False, hold=True, reason=decision.reasoning, metadata=metadata)
        # "block"
        return EvaluationResult(allow=False, hold=False, reason=decision.reasoning, metadata=metadata)
