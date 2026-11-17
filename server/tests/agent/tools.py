"""Tools used by the agent-loop tests. Each records its calls so tests can see what really ran."""

import asyncio

from pydantic import BaseModel, ConfigDict

from enzo.tools import ToolRegistry, tool


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class Out(BaseModel):
    value: str


ran: list[tuple[str, str]] = []


@tool(name="step", description="Record a step.")
async def step(args: Args) -> Out:
    ran.append(("step", args.value))
    await asyncio.sleep(0.1)
    return Out(value=f"did {args.value}")


@tool(name="boom", description="Always fails.")
async def boom(args: Args) -> Out:
    ran.append(("boom", args.value))
    msg = "exploded"
    raise RuntimeError(msg)


@tool(name="send_email", description="Send an email.", requires_approval=True)
async def send_email(args: Args) -> Out:
    ran.append(("send_email", args.value))
    return Out(value="sent")


def registry() -> ToolRegistry:
    ran.clear()
    return ToolRegistry([step, boom, send_email])
