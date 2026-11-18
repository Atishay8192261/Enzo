"""Brain configuration: which brains exist and how tasks route to them (spec §1.3.3, ADR 0010).

Routing lives in a file, not in code, so it can change without a redeploy and is visible in review.
API keys never live here: each brain names the environment variable that holds its key.

Every brain has a tier. Tasks map to tiers, and the router picks the brain of that tier from the
providers the user connected, so no provider is favoured.
"""

import tomllib
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from enzo.brain.pricing import Pricing

type TaskKind = Literal["light", "hard", "streaming"]
type Tier = Literal["fast", "balanced", "powerful"]
TIERS: tuple[Tier, ...] = ("fast", "balanced", "powerful")
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
    #: fast: cheapest, lowest latency · balanced: everyday default · powerful: hardest tasks.
    tier: Tier
    #: Enzo's pick for this provider in this tier (shown to users, chosen first by routing).
    recommended: bool = False
    #: Why it is or is not recommended, shown in the catalog. Keep it factual and sourced.
    note: str | None = None

    @model_validator(mode="after")
    def _effort_is_valid(self) -> Self:
        if self.api == "anthropic" and self.effort and self.effort not in ANTHROPIC_EFFORTS:
            msg = f"anthropic effort must be one of {ANTHROPIC_EFFORTS}"
            raise ValueError(msg)
        return self

    @property
    def provider_name(self) -> str:
        return self.provider or ("anthropic" if self.api == "anthropic" else "openai")


class ClassifyConfig(_Strict):
    """Rules that pick a turn's task before any brain is called: explicit, logged, free."""

    #: A new message longer than this (a pasted document) is a hard task: long-document reasoning.
    long_message_chars: int = Field(default=20_000, gt=0)


class RoutingConfig(_Strict):
    #: Tier for any task not listed in ``tasks``.
    default: Tier = "balanced"
    tasks: dict[TaskKind, Tier] = Field(default_factory=dict)
    classify: ClassifyConfig = Field(default_factory=ClassifyConfig)


class BrainsConfig(_Strict):
    brains: dict[BrainName, BrainConfig] = Field(min_length=1)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)

    @model_validator(mode="after")
    def _names_are_unambiguous(self) -> Self:
        clash = sorted(set(self.brains) & set(TIERS))
        if clash:  # users choose "/model <brain or tier>": a brain may not be named like a tier
            msg = f"brain names cannot be tier names: {', '.join(clash)}"
            raise ValueError(msg)
        return self

    def tier_of(self, task: TaskKind) -> Tier:
        return self.routing.tasks.get(task, self.routing.default)


def load_config(path: Path | None = None) -> BrainsConfig:
    """Load and validate a brains file; with no path, the default shipped with Enzo."""
    if path is None:
        text = resources.files("enzo.brain").joinpath("brains.toml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return BrainsConfig.model_validate(tomllib.loads(text))
