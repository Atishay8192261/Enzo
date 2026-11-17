"""Swappable LLM brain (spec §1).

Everything outside ``enzo.brain.adapters`` talks to a brain only through the canonical types and the
``BrainProvider`` protocol defined here, never through a provider's own request or response shapes.
"""

from enzo.brain.errors import (
    AuthError,
    BadRequestError,
    BrainError,
    ProviderConnectionError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
    UnsupportedCapabilityError,
)
from enzo.brain.models import (
    BrainResponse,
    Capabilities,
    Message,
    NativePart,
    StreamDone,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCall,
    ToolChoice,
    ToolResult,
    ToolSpec,
    Usage,
)
from enzo.brain.provider import BrainProvider, tool_call
from enzo.brain.toolcall import ToolCallRejection, ValidatedCall, validate_call

__all__ = [
    "AuthError",
    "BadRequestError",
    "BrainError",
    "BrainProvider",
    "BrainResponse",
    "Capabilities",
    "Message",
    "NativePart",
    "ProviderConnectionError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "RateLimitError",
    "StreamDone",
    "StreamEvent",
    "TextDelta",
    "TextPart",
    "ToolCall",
    "ToolCallRejection",
    "ToolChoice",
    "ToolResult",
    "ToolSpec",
    "UnsupportedCapabilityError",
    "Usage",
    "ValidatedCall",
    "tool_call",
    "validate_call",
]
