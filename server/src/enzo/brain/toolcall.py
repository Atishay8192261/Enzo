"""Validate tool calls against their schema before anything executes (spec §4.2, §7.2.1)."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from enzo.brain.models import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ValidatedCall:
    call: ToolCall
    args: BaseModel


@dataclass(frozen=True, slots=True)
class ToolCallRejection:
    """A call that must not run. ``as_result`` makes it an error result for the brain to fix."""

    call: ToolCall
    reason: str

    def as_result(self) -> ToolResult:
        return ToolResult(
            call_id=self.call.id, name=self.call.name, content=self.reason, is_error=True
        )


def validate_call(
    call: ToolCall, params: Mapping[str, type[BaseModel]]
) -> ValidatedCall | ToolCallRejection:
    """Strictly parse a call's raw JSON arguments. No type coercion, no unknown fields."""
    model = params.get(call.name)
    if model is None:
        available = ", ".join(sorted(params)) or "none"
        return ToolCallRejection(call, f"Unknown tool '{call.name}'. Available tools: {available}.")
    try:
        args = model.model_validate_json(call.arguments_json, strict=True)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        )
        return ToolCallRejection(call, f"Invalid arguments for {call.name}: {problems}.")
    return ValidatedCall(call, args)
