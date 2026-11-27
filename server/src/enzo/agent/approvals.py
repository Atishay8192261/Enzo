"""Approval gating keyed to the exact action of one request (spec §3 step 5, §12.2, §12.3).

An approval covers one tool call with one exact set of arguments. If any argument changes, the id
changes, so Enzo asks again. The same action asked for again later is a new request with a new id:
an earlier yes or no never decides it (ADR 0014). A claim of past consent is not consent: only a
recorded decision for this id counts.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from enzo.brain.models import ToolCall


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    call: ToolCall
    tool: str
    arguments: dict[str, Any]
    #: The exact action in plain words, for the approval card. Not part of the approval id: the
    #: id covers the tool and its exact arguments, which are what gets approved.
    summary: str | None = None
    #: What the action will change, shown with it (e.g. the diff a merge would land).
    detail: str | None = None
    #: Private details the action includes, in plain lines for the card ("This includes a
    #: health detail: …", ADR 0029). Not part of the id: they describe the exact arguments.
    privacy: tuple[str, ...] = ()


def approval_request(
    call: ToolCall,
    args: BaseModel,
    summary: str | None = None,
    detail: str | None = None,
    *,
    privacy: Sequence[str] = (),
) -> ApprovalRequest:
    arguments = args.model_dump(mode="json")
    canonical = json.dumps(
        {"call": call.id, "tool": call.name, "arguments": arguments}, sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return ApprovalRequest(
        id=f"appr_{digest}",
        call=call,
        tool=call.name,
        arguments=arguments,
        summary=summary,
        detail=detail,
        privacy=tuple(privacy),
    )
