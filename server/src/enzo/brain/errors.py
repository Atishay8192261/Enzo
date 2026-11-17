"""Provider-neutral errors. Adapters map every provider failure onto exactly one of these."""

from typing import ClassVar


class BrainError(Exception):
    """Base error. Carries provider and model so each failure is logged per spec §1.3.4."""

    kind: ClassVar[str] = "error"
    #: Only these failures may trigger the single fallback attempt (spec §1.3.4).
    fallback_eligible: ClassVar[bool] = False

    def __init__(self, message: str, *, provider: str, model: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class RateLimitError(BrainError):
    kind = "rate_limit"
    fallback_eligible = True


class ProviderServerError(BrainError):
    kind = "server_error"
    fallback_eligible = True


class ProviderTimeoutError(BrainError):
    kind = "timeout"
    fallback_eligible = True


class AuthError(BrainError):
    kind = "auth"


class BadRequestError(BrainError):
    kind = "bad_request"


class UnsupportedCapabilityError(BrainError):
    """The brain does not offer this capability. Never emulate it silently (spec §1.3.2)."""

    kind = "unsupported"
