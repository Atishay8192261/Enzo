"""Pick a brain per task, try one recorded fallback, account for every call (spec §1.3.3 to §1.3.5).

Rules this module enforces:
- the route comes from configuration and is logged: task → tier → the tier's brain from the
  providers the user connected (ADR 0010);
- a conversation stays with one provider (stable hash), so different conversations spread across
  providers and prompt caches stay warm;
- the fallback is the next connected provider's brain of the same tier, never a weaker one when a
  same-tier brain exists;
- only rate-limit, server, connection and timeout failures fall back, exactly once;
- a stream only falls back before its first token, so the user never sees two half-answers;
- every attempt, failed or not, becomes a ``CallRecord`` with tokens, latency and cost (or None).
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Protocol

from enzo.brain.config import TIERS, BrainConfig, BrainsConfig, TaskKind, Tier
from enzo.brain.errors import BrainError, ProviderServerError
from enzo.brain.factory import build_brain, has_key
from enzo.brain.models import (
    BrainResponse,
    Message,
    StreamDone,
    TextDelta,
    TextPart,
    ToolChoice,
    ToolSpec,
)
from enzo.brain.pricing import cost_usd
from enzo.brain.provider import BrainProvider

log = logging.getLogger("enzo.brain.router")


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One brain call, as spec §1.3.5 requires it to be recorded."""

    brain: str
    provider: str
    model: str
    task: str
    attempt: int
    started_at: datetime
    latency_ms: float
    outcome: str  # "ok" or the canonical error kind
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    cost_usd: float | None = None  # None means the provider publishes no price: unknown, not free
    error: str | None = None
    fallback_from: str | None = None
    #: "plan": paid by the user's subscription (a ChatGPT sign-in), so no per-token cost exists.
    billing: Literal["usage", "plan"] = "usage"


class Ledger(Protocol):
    def record(self, record: CallRecord) -> None: ...


@dataclass
class InMemoryLedger:
    records: list[CallRecord] = field(default_factory=list)

    def record(self, record: CallRecord) -> None:
        self.records.append(record)


@dataclass(frozen=True, slots=True)
class RoutePlan:
    task: TaskKind
    tier: Tier
    primary: str
    fallback: str | None
    reason: str


#: Nearest tiers first when a provider has no brain in the asked tier: stronger before weaker.
_NEAREST: dict[Tier, tuple[Tier, ...]] = {
    "fast": ("fast", "balanced", "powerful"),
    "balanced": ("balanced", "powerful", "fast"),
    "powerful": ("powerful", "balanced", "fast"),
}


class NoBrainAvailableError(BrainError):
    """No connected account can serve this request. Nothing was sent to any provider."""

    kind = "not_connected"

    def __init__(self, message: str) -> None:
        super().__init__(message, provider="none", model="none")


@dataclass(frozen=True, slots=True)
class RoutedResponse:
    response: BrainResponse
    brain: str
    attempts: tuple[CallRecord, ...]


@dataclass(frozen=True, slots=True)
class RoutedDone:
    """Last event of a routed stream."""

    routed: RoutedResponse


class AllBrainsFailedError(Exception):
    """The primary and its fallback both failed. Lists what was tried (spec §1.3.4 step 3)."""

    def __init__(self, attempts: Sequence[CallRecord]) -> None:
        self.attempts = tuple(attempts)
        tried = "; ".join(
            f"{a.brain} ({a.provider}/{a.model}): {a.outcome}: {a.error}" for a in self.attempts
        )
        super().__init__(f"all brains failed: {tried}")


class PromptOverBudgetError(Exception):
    """The prompt plus reserved output exceeds the task budget: compact first (spec §1.3.5, §5)."""

    def __init__(
        self, *, brain: str, prompt_tokens: int, max_output_tokens: int, budget: int
    ) -> None:
        self.brain = brain
        self.prompt_tokens = prompt_tokens
        self.max_output_tokens = max_output_tokens
        self.budget = budget
        super().__init__(
            f"{brain}: prompt {prompt_tokens} + output reserve {max_output_tokens} "
            f"exceeds budget {budget}"
        )


type BrainFactory = Callable[[BrainConfig], BrainProvider]
#: Whether a brain's provider has credentials (a connected account or an environment key).
type Availability = Callable[[BrainConfig], bool]


def classify(config: BrainsConfig, messages: Sequence[Message]) -> tuple[TaskKind, str]:
    """The task of a turn, from explicit rules and no model call (spec §1.3.3), with the reason.

    The user's own words are the first text part of the latest user message (ADR 0007 puts context
    and memory after them), so a resumed turn classifies exactly like the turn it continues.
    """
    words = next((m.parts[0] for m in reversed(messages) if m.role == "user" and m.parts), None)
    size = len(words.text) if isinstance(words, TextPart) else 0
    limit = config.routing.classify.long_message_chars
    if size > limit:
        return "hard", f"message of {size} characters > {limit}: long-document reasoning"
    return "streaming", "live reply"


def billing_of(brain: BrainProvider) -> Literal["usage", "plan"]:
    """ "plan" for a brain on the user's subscription (Sign in with ChatGPT), else "usage"."""
    return "plan" if getattr(brain, "billing", "usage") == "plan" else "usage"


@dataclass(frozen=True, slots=True)
class _Attempt:
    brain: str
    task: str
    number: int
    primary: str
    started_at: datetime
    started: float
    #: Who pays for this brain, known before the call: a failed or stopped call on the
    #: user's ChatGPT plan is still the plan's, never an unknown per-token cost (#43).
    billing: Literal["usage", "plan"] = "usage"

    @property
    def fallback_from(self) -> str | None:
        return self.primary if self.brain != self.primary else None


class Router:
    def __init__(
        self,
        config: BrainsConfig,
        *,
        build: BrainFactory = build_brain,
        available: Availability = has_key,
        ledger: Ledger | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._build = build
        self._available = available
        self.ledger: Ledger = ledger if ledger is not None else InMemoryLedger()
        self._clock = clock
        self._live: dict[str, BrainProvider] = {}

    @property
    def config(self) -> BrainsConfig:
        return self._config

    def forget(self, provider: str) -> None:
        """Drop built adapters for a provider, so a new or removed key takes effect at once."""
        for name in [n for n in self._live if self._config.brains[n].provider_name == provider]:
            del self._live[name]

    def available(self, name: str) -> bool:
        """Whether this brain's provider has credentials right now."""
        return self._available(self._config.brains[name])

    def brain(self, name: str) -> BrainProvider:
        """Adapters are built on first use, so a missing key only matters for brains in use."""
        if name not in self._live:
            self._live[name] = self._build(self._config.brains[name])
        return self._live[name]

    def plan(
        self, task: TaskKind, *, override: str | None = None, affinity: str | None = None
    ) -> RoutePlan:
        """Choose a primary and one fallback. ``override`` is a brain or a tier the user picked;
        ``affinity`` (the conversation id) keeps a conversation with one provider."""
        brains = self._config.brains
        if override is not None and override not in brains and override not in TIERS:
            msg = f"unknown brain or tier {override!r}"
            raise KeyError(msg)
        order = self._providers(affinity)
        if override in brains:
            chosen = brains[override]
            others = [p for p in order if p != chosen.provider_name]
            backup = self._candidates(chosen.tier, others)
            plan = RoutePlan(
                task,
                chosen.tier,
                override,
                backup[0][0] if backup else None,
                "selected by user",
            )
        else:
            tier: Tier = override if override in TIERS else self._config.tier_of(task)
            candidates = self._candidates(tier, order)
            if not candidates:
                raise NoBrainAvailableError(_nothing_connected(self._config))
            (primary, got), backup = candidates[0], candidates[1:]
            why = "selected by user" if override else f"task {task}"
            reason = f"tier {tier} ({why}); providers in order: {', '.join(order)}"
            if got != tier:
                reason += f"; {brains[primary].provider_name} has no {tier} brain, using {got}"
            plan = RoutePlan(task, tier, primary, backup[0][0] if backup else None, reason)
        log.info(
            "route",
            extra={
                "task": task,
                "tier": plan.tier,
                "primary": plan.primary,
                "fallback": plan.fallback,
                "reason": plan.reason,
            },
        )
        return plan

    def _providers(self, affinity: str | None) -> list[str]:
        """Connected providers, rotated by a stable hash of the conversation: an even spread."""
        names = sorted(
            {c.provider_name for c in self._config.brains.values() if self._available(c)}
        )
        if not names or affinity is None:
            return names
        shift = int.from_bytes(hashlib.sha256(affinity.encode()).digest()[:8]) % len(names)
        return names[shift:] + names[:shift]

    def _candidates(self, tier: Tier, providers: Sequence[str]) -> list[tuple[str, Tier]]:
        """Per provider: its recommended brain of the nearest tier it has. Same or stronger tiers
        come first, in provider order; a weaker brain only when no provider has one (§1.3.4)."""
        out: list[tuple[str, Tier]] = []
        for provider in providers:
            own = [
                (n, c)
                for n, c in self._config.brains.items()
                if c.provider_name == provider and self._available(c)
            ]
            for want in _NEAREST[tier]:
                fits = [n for n, c in own if c.tier == want]
                picks = [n for n in fits if self._config.brains[n].recommended] or fits
                if picks:
                    out.append((picks[0], want))
                    break
        return sorted(out, key=lambda c: TIERS.index(c[1]) < TIERS.index(tier))  # stable

    async def chat(  # noqa: PLR0913 - keyword-only call settings
        self,
        task: TaskKind,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
        override: str | None = None,
        affinity: str | None = None,
        budget_tokens: int | None = None,
    ) -> RoutedResponse:
        done: RoutedDone | None = None
        async for event in self.stream(
            task,
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_output_tokens=max_output_tokens,
            override=override,
            affinity=affinity,
            budget_tokens=budget_tokens,
        ):
            if isinstance(event, RoutedDone):
                done = event
        if done is None:  # stream() always ends in RoutedDone or raises; guard regardless
            msg = "routed stream ended without a result"
            raise RuntimeError(msg)
        return done.routed

    async def stream(  # noqa: PLR0913 - keyword-only call settings
        self,
        task: TaskKind,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
        override: str | None = None,
        affinity: str | None = None,
        budget_tokens: int | None = None,
    ) -> AsyncIterator[TextDelta | RoutedDone]:
        plan = self.plan(task, override=override, affinity=affinity)
        attempts: list[CallRecord] = []
        candidates = [plan.primary] + ([plan.fallback] if plan.fallback else [])
        for number, name in enumerate(candidates, start=1):
            attempt = _Attempt(name, task, number, plan.primary, self._clock(), time.perf_counter())
            emitted = False
            try:
                # Inside the try: a fallback brain with no key must still be recorded as tried.
                brain = self.brain(name)
                attempt = replace(attempt, billing=billing_of(brain))
                if budget_tokens is not None:
                    await self._check_budget(
                        name, brain, messages, tools, max_output_tokens, budget_tokens
                    )
                async for event in brain.stream(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_output_tokens=max_output_tokens,
                ):
                    if isinstance(event, TextDelta):
                        emitted = True
                        yield event
                    elif isinstance(event, StreamDone):
                        attempts.append(self._record_ok(attempt, event.response))
                        yield RoutedDone(RoutedResponse(event.response, name, tuple(attempts)))
                        return
                msg = "stream ended without a final response"
                raise ProviderServerError(msg, provider=brain.provider, model=brain.model)
            except asyncio.CancelledError:
                # Stopped by the user (or the turn was cancelled): still an attempt on record.
                self._record_cancelled(attempt)
                raise
            except BrainError as exc:
                attempts.append(self._record_error(attempt, exc))
                # Fall back once, only on transient failures, and never after text reached the user.
                if exc.fallback_eligible and not emitted and number < len(candidates):
                    log.warning(
                        "fallback", extra={"from": name, "to": candidates[number], "kind": exc.kind}
                    )
                    continue
                if number == 1:
                    exc.attempts = tuple(attempts)
                    raise
                raise AllBrainsFailedError(attempts) from exc

    async def _check_budget(
        self,
        name: str,
        brain: BrainProvider,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_output_tokens: int,
        budget: int,
    ) -> None:
        prompt = await brain.count_tokens(messages, tools=tools)
        if prompt + max_output_tokens > budget:
            raise PromptOverBudgetError(
                brain=name, prompt_tokens=prompt, max_output_tokens=max_output_tokens, budget=budget
            )

    def _record_ok(self, attempt: _Attempt, response: BrainResponse) -> CallRecord:
        usage = response.usage
        record = CallRecord(
            brain=attempt.brain,
            provider=response.provider,
            model=response.model,
            task=attempt.task,
            attempt=attempt.number,
            started_at=attempt.started_at,
            latency_ms=response.latency_ms,
            outcome="ok",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_input_tokens=usage.cache_write_input_tokens,
            cost_usd=(
                None
                if response.billing == "plan"
                else cost_usd(usage, self._config.brains[attempt.brain].pricing)
            ),
            fallback_from=attempt.fallback_from,
            billing=response.billing,
        )
        self._emit(record)
        return record

    def _record_error(self, attempt: _Attempt, exc: BrainError) -> CallRecord:
        record = CallRecord(
            brain=attempt.brain,
            provider=exc.provider,
            model=exc.model,
            task=attempt.task,
            attempt=attempt.number,
            started_at=attempt.started_at,
            latency_ms=(time.perf_counter() - attempt.started) * 1000,
            outcome=exc.kind,
            error=str(exc),
            fallback_from=attempt.fallback_from,
            billing=attempt.billing,
        )
        self._emit(record)
        return record

    def _record_cancelled(self, attempt: _Attempt) -> CallRecord:
        cfg = self._config.brains[attempt.brain]
        record = CallRecord(
            brain=attempt.brain,
            provider=cfg.provider_name,
            model=cfg.model,
            task=attempt.task,
            attempt=attempt.number,
            started_at=attempt.started_at,
            latency_ms=(time.perf_counter() - attempt.started) * 1000,
            outcome="cancelled",
            fallback_from=attempt.fallback_from,
            billing=attempt.billing,
        )
        self._emit(record)
        return record

    def _emit(self, record: CallRecord) -> None:
        self.ledger.record(record)
        log.info("brain call", extra={"call": asdict(record)})


def _nothing_connected(config: BrainsConfig) -> str:
    # In catalog order (the defaults' providers come first), not alphabetical (#25).
    providers = list(dict.fromkeys(c.provider_name for c in config.brains.values()))
    logins = ", ".join(f"/login {p}" for p in providers)
    return f"no model account is connected. Connect one: {logins}"
