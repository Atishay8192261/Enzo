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
    #: The model's context window and longest reply, in tokens, as its provider publishes them.
    #: Left out when unconfirmed: the brain is then budgeted as ``context.default_window``.
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    #: Where and when the limits were read (required with them, like a price's source).
    limits_source: str | None = None
    #: Characters per token for the local estimate; default: ``context.chars_per_token``.
    chars_per_token: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _effort_is_valid(self) -> Self:
        if self.api == "anthropic" and self.effort and self.effort not in ANTHROPIC_EFFORTS:
            msg = f"anthropic effort must be one of {ANTHROPIC_EFFORTS}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _limits_are_sourced(self) -> Self:
        if (self.context_window or self.max_output_tokens) and not self.limits_source:
            msg = "context_window and max_output_tokens need limits_source (where they were read)"
            raise ValueError(msg)
        return self

    @property
    def provider_name(self) -> str:
        return self.provider or ("anthropic" if self.api == "anthropic" else "openai")


class ClassifyConfig(_Strict):
    """Rules that pick a turn's task before any brain is called: explicit, logged, free."""

    #: A new message longer than this (a pasted document) is a hard task: long-document reasoning.
    long_message_chars: int = Field(default=20_000, gt=0)


class ContextConfig(_Strict):
    """How every brain's context window is split (spec §1.3.5, §5.2; ADR 0024).

    Configuration, not guessed per call. Of a window, ``margin`` is never filled, the reply's
    tokens are reserved, and the rest is the prompt's hard limit. At the start of a turn the prompt
    must also leave ``task_share`` of that limit free for the new task and stay under ``compact_at``
    (and under the brain's long-context price step); otherwise older turns are summarized first.
    """

    margin: float = Field(default=0.10, ge=0, lt=0.5)
    reply_tokens: int = Field(default=16_000, gt=0)
    task_share: float = Field(default=0.25, ge=0, lt=0.9)
    compact_at: int = Field(default=150_000, gt=0)
    #: Newest turns kept word for word after a compaction (at most a third of the trigger).
    keep_recent: int = Field(default=30_000, ge=0)
    #: The longest summary: the summarizer's ``max_output_tokens``.
    summary_tokens: int = Field(default=16_000, gt=0)
    #: Which tier of the conversation's own provider writes summaries (never another provider).
    summary_tier: Tier = "fast"
    #: Counting: a local estimate first; past this share of a limit, the provider's exact count.
    exact_above: float = Field(default=0.8, gt=0, le=1)
    #: A brain without a confirmed window is budgeted as if it had this one.
    default_window: int = Field(default=128_000, gt=0)
    #: Characters per token of the local estimate, by provider; "default" for any other.
    chars_per_token: dict[str, float] = Field(default_factory=lambda: {"default": 2.0})
    #: A tool result longer than this is stored as a file: the context keeps its start, a pointer.
    tool_inline_chars: int = Field(default=20_000, gt=0)
    #: A message longer than this (a pasted document) is treated the same way.
    paste_inline_chars: int = Field(default=100_000, gt=0)
    #: How much of a stored text stays in the context.
    excerpt_chars: int = Field(default=4_000, gt=0)

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        if "default" not in self.chars_per_token or min(self.chars_per_token.values()) <= 0:
            msg = "context.chars_per_token needs a positive 'default' entry"
            raise ValueError(msg)
        if self.excerpt_chars >= min(self.tool_inline_chars, self.paste_inline_chars):
            msg = "context.excerpt_chars must be smaller than the inline limits"
            raise ValueError(msg)
        return self

    def ratio(self, brain: BrainConfig) -> float:
        """Characters per token of this brain's local estimate."""
        return brain.chars_per_token or self.chars_per_token.get(
            brain.provider_name, self.chars_per_token["default"]
        )


class RoutingConfig(_Strict):
    #: Tier for any task not listed in ``tasks``.
    default: Tier = "balanced"
    tasks: dict[TaskKind, Tier] = Field(default_factory=dict)
    classify: ClassifyConfig = Field(default_factory=ClassifyConfig)


class BrainsConfig(_Strict):
    brains: dict[BrainName, BrainConfig] = Field(min_length=1)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

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
