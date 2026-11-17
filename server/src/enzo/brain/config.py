"""Brain configuration: which brains exist and how tasks route to them (spec §1.3.3).

Routing lives in a file, not in code, so it can change without a redeploy and is visible in review.
API keys never live here: each brain names the environment variable that holds its key.
"""

import tomllib
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from enzo.brain.pricing import Pricing

type TaskKind = Literal["light", "hard", "streaming"]
type Api = Literal["anthropic", "openai-responses"]

ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")
type BrainName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BrainConfig(_Strict):
    api: Api
    model: str = Field(min_length=1)
    #: Provider name recorded in logs and used for replay; defaults from ``api``.
    provider: str | None = None
    base_url: str | None = None
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    effort: str | None = None
    forced_tool_choice: bool | None = None
    embedding_model: str | None = None
    pricing: Pricing | None = None

    @model_validator(mode="after")
    def _effort_is_valid(self) -> Self:
        if self.api == "anthropic" and self.effort and self.effort not in ANTHROPIC_EFFORTS:
            msg = f"anthropic effort must be one of {ANTHROPIC_EFFORTS}"
            raise ValueError(msg)
        return self

    @property
    def provider_name(self) -> str:
        return self.provider or ("anthropic" if self.api == "anthropic" else "openai")


class RoutingConfig(_Strict):
    default: str
    tasks: dict[TaskKind, str] = Field(default_factory=dict)
    #: One fallback per brain, tried once (spec §1.3.4).
    fallback: dict[str, str] = Field(default_factory=dict)


class BrainsConfig(_Strict):
    brains: dict[BrainName, BrainConfig] = Field(min_length=1)
    routing: RoutingConfig

    @model_validator(mode="after")
    def _names_resolve(self) -> Self:
        referenced = [self.routing.default, *self.routing.tasks.values()]
        referenced += [*self.routing.fallback, *self.routing.fallback.values()]
        unknown = sorted({n for n in referenced if n not in self.brains})
        if unknown:
            msg = f"routing refers to unknown brains: {', '.join(unknown)}"
            raise ValueError(msg)
        for source, target in self.routing.fallback.items():
            if source == target:
                msg = f"brain {source!r} cannot fall back to itself"
                raise ValueError(msg)
        return self


def load_config(path: Path | None = None) -> BrainsConfig:
    """Load and validate a brains file; with no path, the default shipped with Enzo."""
    if path is None:
        text = resources.files("enzo.brain").joinpath("brains.toml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return BrainsConfig.model_validate(tomllib.loads(text))
