import pytest

from enzo.brain import ToolCall, ToolCallRejection, ValidatedCall, validate_call
from tests.brain.schemas import SearchWeb

PARAMS = {"search_web": SearchWeb}


def call(args: str, name: str = "search_web") -> ToolCall:
    return ToolCall(id="c1", name=name, arguments_json=args)


def test_valid_call_returns_typed_arguments() -> None:
    result = validate_call(call('{"query": "flights", "max_results": 3}'), PARAMS)
    assert isinstance(result, ValidatedCall)
    assert result.args == SearchWeb(query="flights", max_results=3)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ('{"query": "x", "max_results": "3"}', "max_results: Input should be a valid integer"),
        ('{"query": "x", "bogus": 1}', "bogus: Extra inputs are not permitted"),
        ('{"max_results": 3}', "query: Field required"),
        ('{"query": "x", "max_results": 99}', "max_results: Input should be less than or equal"),
        ('{"query": "x"', "Invalid JSON"),
    ],
)
def test_bad_arguments_are_rejected_before_execution(args: str, expected: str) -> None:
    result = validate_call(call(args), PARAMS)
    assert isinstance(result, ToolCallRejection)
    assert expected in result.reason


def test_unknown_tool_names_the_available_ones() -> None:
    result = validate_call(call("{}", name="delete_everything"), PARAMS)
    assert isinstance(result, ToolCallRejection)
    assert result.reason == "Unknown tool 'delete_everything'. Available tools: search_web."


def test_rejection_becomes_an_error_result_for_the_brain() -> None:
    rejection = validate_call(call('{"query": 1}'), PARAMS)
    assert isinstance(rejection, ToolCallRejection)
    result = rejection.as_result()
    assert (result.call_id, result.name, result.is_error) == ("c1", "search_web", True)
    assert "query" in result.content
