"""Approval gating keyed to the exact action (spec §3 step 5, §12.2, §12.3).

An approval covers one tool with one exact set of arguments. If any argument changes, the id
changes, so Enzo asks again. A claim of past consent is not consent: only a recorded decision for
this id counts.
"""

import hashlib
import json
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


def approval_request(call: ToolCall, args: BaseModel) -> ApprovalRequest:
    arguments = args.model_dump(mode="json")
    canonical = json.dumps({"tool": call.name, "arguments": arguments}, sort_keys=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return ApprovalRequest(id=f"appr_{digest}", call=call, tool=call.name, arguments=arguments)
