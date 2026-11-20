"""The approval card is the one confirmation (#42): nothing tells the model to ask in chat first,
and everything it reads about a gated action says to call it directly."""

from pydantic import BaseModel, ConfigDict

from enzo.agent.service import DEFAULT_SYSTEM_PROMPT
from enzo.tools import ToolRegistry, tool
from enzo.tools.registry import APPROVAL_NOTE, Tool
from tests.agent import tools as t


def test_the_system_prompt_says_the_card_is_the_only_confirmation() -> None:
    prompt = " ".join(DEFAULT_SYSTEM_PROMPT.split())
    assert "call the tool directly" in prompt
    assert "that card is the only confirmation" in prompt
    assert 'Never ask "should I…?" or "please confirm" in chat first' in prompt


def test_every_gated_tool_tells_the_model_to_call_it_directly() -> None:
    specs = {s.name: s.description for s in t.registry().specs()}
    assert specs["send_email"].endswith(APPROVAL_NOTE)  # gated
    assert APPROVAL_NOTE not in specs["step"]  # not gated: nothing to confirm
    assert specs["send_email"].startswith("Send an email.")  # the tool's own words stay first


def test_tools_described_by_a_schema_get_it_too() -> None:
    """Connector tools (MCP) come with a JSON Schema instead of a model."""

    class Out(BaseModel):
        model_config = ConfigDict(extra="forbid")
        ok: bool

    async def handler(_args: BaseModel) -> Out:
        return Out(ok=True)

    merge = Tool(
        name="github_merge",
        description="GitHub: Merge a pull request",
        params=BaseModel,
        returns=Out,
        handler=handler,
        requires_approval=True,
        schema={"type": "object", "properties": {}},
    )
    (spec,) = ToolRegistry([merge]).specs()
    assert spec.description == f"GitHub: Merge a pull request {APPROVAL_NOTE}"


def test_the_note_never_reaches_an_ungated_tool() -> None:
    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        q: str

    @tool(name="lookup", description="Look something up.")
    async def lookup(args: Args) -> Args:
        return args

    assert lookup.spec().description == "Look something up."
