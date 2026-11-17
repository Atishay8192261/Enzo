"""Pick a brain per task, try one recorded fallback, account for every call (spec §1.3.3 to §1.3.5).

Rules this module enforces:
- the route comes from configuration and is logged;
- only rate-limit, server, connection and timeout failures fall back, exactly once;
- a stream only falls back before its first token, so the user never sees two half-answers;
- every attempt, failed or not, becomes a ``CallRecord`` with tokens, latency and cost (or None).
"""

import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from enzo.brain.config import BrainConfig, BrainsConfig, TaskKind
from enzo.brain.errors import BrainError, ProviderServerError
from enzo.brain.factory import build_brain
from enzo.brain.models import BrainResponse, Message, StreamDone, TextDelta, ToolChoice, ToolSpec
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
    primary: str
    fallback: str | None
    reason: str


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


@dataclass(frozen=True, slots=True)
class _Attempt:
    brain: str
    task: str
    number: int
    primary: str
    started_at: datetime
    started: float

    @property
    def fallback_from(self) -> str | None:
        return self.primary if self.brain != self.primary else None


class Router:
    def __init__(
        self,
        config: BrainsConfig,
        *,
        build: BrainFactory = build_brain,
        ledger: Ledger | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._build = build
        self.ledger: Ledger = ledger if ledger is not None else InMemoryLedger()
        self._clock = clock
        self._live: dict[str, BrainProvider] = {}

    @property
    def config(self) -> BrainsConfig:
        return self._config

    def brain(self, name: str) -> BrainProvider:
        """Adapters are built on first use, so a missing key only matters for brains in use."""
        if name not in self._live:
            self._live[name] = self._build(self._config.brains[name])
        return self._live[name]

    def plan(self, task: TaskKind, *, override: str | None = None) -> RoutePlan:
        routing = self._config.routing
        if override is not None:
            if override not in self._config.brains:
                msg = f"unknown brain {override!r}"
                raise KeyError(msg)
            primary, reason = override, "selected by user"
        elif task in routing.tasks:
            primary, reason = routing.tasks[task], f"routing.tasks.{task}"
        else:
            primary, reason = routing.default, "routing.default"
        plan = RoutePlan(task, primary, routing.fallback.get(primary), reason)
        log.info(
            "route",
            extra={"task": task, "primary": primary, "fallback": plan.fallback, "reason": reason},
        )
        return plan

    async def chat(
        self,
        task: TaskKind,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
        override: str | None = None,
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
            budget_tokens=budget_tokens,
        ):
            if isinstance(event, RoutedDone):
                done = event
        if done is None:  # stream() always ends in RoutedDone or raises; guard regardless
            msg = "routed stream ended without a result"
            raise RuntimeError(msg)
        return done.routed

    async def stream(
        self,
        task: TaskKind,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
        override: str | None = None,
        budget_tokens: int | None = None,
    ) -> AsyncIterator[TextDelta | RoutedDone]:
        plan = self.plan(task, override=override)
        attempts: list[CallRecord] = []
        candidates = [plan.primary] + ([plan.fallback] if plan.fallback else [])
        for number, name in enumerate(candidates, start=1):
            attempt = _Attempt(name, task, number, plan.primary, self._clock(), time.perf_counter())
            emitted = False
            try:
                # Inside the try: a fallback brain with no key must still be recorded as tried.
                brain = self.brain(name)
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
            cost_usd=cost_usd(usage, self._config.brains[attempt.brain].pricing),
            fallback_from=attempt.fallback_from,
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
        )
        self._emit(record)
        return record

    def _emit(self, record: CallRecord) -> None:
        self.ledger.record(record)
        log.info("brain call", extra={"call": asdict(record)})
