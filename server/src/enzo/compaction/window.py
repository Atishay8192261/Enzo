"""Before every brain call: count the prompt, and summarize older turns when it is over budget.

Spec §1.3.5: the budget is enforced before calling the brain; an oversized prompt is never sent.
Spec §5.2: count before every call (mid-turn tool steps too), compact when over, recent turns
verbatim, the split from configuration (``enzo.compaction.budget``).

When a turn starts (the prompt ends with the user's new message), a prompt over the budget's
``compact_at`` is compacted first: one cache reset, then append-only again. Mid-turn only the hard
limit compacts (that also drops the current tool round's replayed reasoning; see ADR 0024).

The summary is written by the conversation's own provider (its ``summary_tier`` brain, else the
conversation's brain), never another provider, with no fallback. If writing it fails:
- and the prompt still fits the hard limit: the reply uses the whole chat, the failure is logged
  and shown ("couldn't summarize");
- otherwise the turn stops with an honest "too long" report. Nothing oversized is sent.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import cast

from enzo.brain.config import TaskKind
from enzo.brain.errors import BrainError
from enzo.brain.models import Message, ToolSpec
from enzo.brain.provider import BrainProvider
from enzo.brain.router import AllBrainsFailedError, CallRecord, RoutePlan, Router
from enzo.brain.tokens import Count, TokenCounter, text_tokens
from enzo.brain.toolcall import ToolCallRejection, validate_call
from enzo.compaction.budget import Budget
from enzo.compaction.summary import (
    NOTES_TOOL,
    SYSTEM,
    TOOL,
    Folded,
    NotesParams,
    Summary,
    SummaryError,
    check,
    fold,
    merge,
    previous_notes,
)
from enzo.compaction.view import (
    Compaction,
    build_view,
    split_lead,
    turn_positions,
    without_replay,
)
from enzo.tools.stored import OutputStore

log = logging.getLogger("enzo.compaction")


@dataclass(frozen=True, slots=True)
class Compacted:
    """Older turns were summarized before this call (or summarizing failed, see ``failed``)."""

    #: Turns the summary now covers, and turns the prompt still holds word for word.
    turns: int
    kept: int
    #: The prompt's size before and after, in tokens.
    before: int
    after: int
    #: Why summarizing failed; the call went ahead with the whole chat, which still fit.
    failed: str | None = None


@dataclass(frozen=True, slots=True)
class Fit:
    """What to send: the prompt within budget, and what it took to get there."""

    messages: list[Message]
    #: Summary calls made for it, for the ledger (spec §1.3.5).
    calls: tuple[CallRecord, ...] = ()
    compacted: Compacted | None = None


class ContextOverflowError(Exception):
    """The prompt cannot be made to fit: the turn stops honestly instead of sending it."""

    def __init__(
        self,
        message: str,
        calls: Sequence[CallRecord] = (),
        compacted: "Compacted | None" = None,
    ) -> None:
        super().__init__(message)
        self.calls = tuple(calls)
        #: Older turns were summarized (and saved) before it still did not fit.
        self.compacted = compacted


class _NothingToSummarizeError(Exception):
    pass


class ContextWindow:
    """The context of one turn of one conversation. ``made`` holds new summaries to save."""

    def __init__(
        self,
        router: Router,
        counter: TokenCounter,
        *,
        record: Compaction | None = None,
        store: OutputStore | None = None,
    ) -> None:
        self._router = router
        self._counter = counter
        self._store = store
        self._ctx = router.config.context
        self.record = record
        self.made: list[Compaction] = []

    def view(self, messages: Sequence[Message]) -> list[Message]:
        return build_view(
            messages,
            self.record,
            paste_limit=self._store.paste_inline_chars if self._store else None,
            shorten=self._shorten_paste if self._store else None,
        )

    def learn(
        self, brain: str, sent: Sequence[Message], tools: Sequence[ToolSpec], reported: int
    ) -> None:
        """A reply's usage report: how big the prompt really was, for later estimates."""
        self._counter.learn(brain, sent, tools, reported)

    async def fit(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        task: TaskKind,
        override: str | None,
        affinity: str | None,
        reply: int,
    ) -> Fit:
        """The prompt to send for ``messages``: within budget, compacted first if needed."""
        plan = self._router.plan(task, override=override, affinity=affinity, quiet=True)
        budget = self._budget(plan, reply)
        view = self.view(messages)
        fresh = bool(messages) and messages[-1].role == "user"  # a new turn starts here
        bound = budget.compact_at if fresh else budget.limit
        before = await self._count(plan.primary, view, tools, bound)
        if before.tokens <= bound:
            return Fit(view)
        calls: list[CallRecord] = []
        try:
            record = await self._compact(messages, plan.primary, budget, calls)
        except (SummaryError, BrainError, AllBrainsFailedError, _NothingToSummarizeError) as exc:
            why = _plain(exc)
            log.error(
                "context.compact failed",
                extra={"brain": plan.primary, "tokens": before.tokens, "error": str(exc)},
            )
            if before.tokens <= budget.limit:
                return Fit(view, tuple(calls), Compacted(0, 0, before.tokens, before.tokens, why))
            raise ContextOverflowError(_too_long(before, budget, why), calls) from exc
        self.record = record
        view = self.view(messages)
        after = await self._count(plan.primary, view, tools, budget.limit)
        self.record = record = replace(record, before=before.tokens, after=after.tokens)
        self.made.append(record)
        kept = turn_positions(split_lead(messages)[1])[-1][0] - record.turns + 1
        log.info(
            "context.compact",
            extra={
                "brain": record.brain,
                "turns": record.turns,
                "kept": kept,
                "before": before.tokens,
                "after": after.tokens,
                "exact": after.exact,
            },
        )
        compacted = Compacted(record.turns, kept, before.tokens, after.tokens)
        if after.tokens > budget.limit:
            why = "even the newest messages alone are too long"
            raise ContextOverflowError(_too_long(after, budget, why), calls, compacted)
        return Fit(view, tuple(calls), compacted)

    def _budget(self, plan: RoutePlan, reply: int) -> Budget:
        brains = self._router.config.brains
        budget = Budget.of(brains[plan.primary], self._ctx, reply=reply)
        if plan.fallback is not None:  # the fallback must be able to take the same prompt
            budget = budget.tighter(Budget.of(brains[plan.fallback], self._ctx, reply=reply))
        return budget

    async def _count(
        self, brain: str, messages: Sequence[Message], tools: Sequence[ToolSpec], limit: int
    ) -> Count:
        return await self._counter.count(
            brain, messages, tools, limit=limit, provider=lambda: self._provider(brain)
        )

    def _provider(self, brain: str) -> BrainProvider | None:
        try:
            return self._router.brain(brain)
        except BrainError:  # e.g. its account was just disconnected: the estimate stands
            return None

    async def _compact(
        self, messages: Sequence[Message], primary: str, budget: Budget, calls: list[CallRecord]
    ) -> Compaction:
        """Summarize the older turns, keeping the newest ``budget.keep`` tokens of whole turns."""
        _, conversation = split_lead(messages)
        positions = turn_positions(conversation)
        turns: dict[int, list[Message]] = {}
        for message, (turn, _) in zip(conversation, positions, strict=True):
            turns.setdefault(turn, []).append(message)
        current = positions[-1][0] if positions else 0
        start = self.record.turns if self.record else 0
        keep_from, kept = current, 0
        for turn in range(current - 1, start - 1, -1):
            size = self._counter.estimate(primary, _plain_turn(turns[turn]), ())
            if kept + size > budget.keep:
                break
            kept += size
            keep_from = turn
        if keep_from == start:  # everything older fits the tail: fold it all to gain room
            keep_from = current
        if keep_from <= start:
            msg = "there are no older messages to summarize"
            raise _NothingToSummarizeError(msg)
        writer = self._summarizer(primary)
        previous = self.record.summary if self.record else None
        summary = await self._summarize(
            writer, previous, {t: turns[t] for t in range(start, keep_from)}, calls
        )
        return Compaction(
            summary=summary,
            turns=keep_from,
            made_turn=current,
            made_offset=len(turns.get(current, [])),
            brain=writer,
            before=0,
            after=0,
        )

    def _summarizer(self, primary: str) -> str:
        """The conversation's own provider writes the summary: its ``summary_tier`` brain if it
        has one available, else the conversation's brain. Never another provider."""
        brains = self._router.config.brains
        provider = brains[primary].provider_name
        own = [
            name
            for name, cfg in brains.items()
            if cfg.provider_name == provider
            and cfg.tier == self._ctx.summary_tier
            and self._router.available(name)
        ]
        picks = [n for n in own if brains[n].recommended] or own
        return picks[0] if picks else primary

    async def _summarize(
        self,
        writer: str,
        previous: Summary | None,
        turns: dict[int, list[Message]],
        calls: list[CallRecord],
    ) -> Summary:
        """Fold the turns into the summary, in as many calls as the writer's window needs."""
        ctx = self._ctx
        cfg = self._router.config.brains[writer]
        budget = Budget.of(cfg, ctx, reply=ctx.summary_tokens)
        ratio = ctx.ratio(cfg)
        # The notes of earlier turns ride along in every call, and so do the instructions.
        fixed = text_tokens(SYSTEM + previous_notes(previous) + NOTES_TOOL.model_dump_json(), ratio)
        room = max(budget.limit - fixed - ctx.summary_tokens, budget.limit // 4)
        summary = previous
        chunk: dict[int, list[Message]] = {}
        size = 0
        for turn, messages in sorted(turns.items()):
            rendered = fold({turn: messages}, self._store, limit=ctx.excerpt_chars).text
            alone = text_tokens(rendered, ratio)
            if chunk and size + alone > room:
                summary = await self._fold_in(writer, summary, chunk, calls)
                chunk, size = {}, 0
            chunk[turn] = messages
            size += alone
        if chunk:
            summary = await self._fold_in(writer, summary, chunk, calls)
        if summary is None:  # unreachable: there was at least one turn to fold
            msg = "nothing was summarized"
            raise SummaryError(msg)
        return summary

    async def _fold_in(
        self,
        writer: str,
        summary: Summary | None,
        chunk: dict[int, list[Message]],
        calls: list[CallRecord],
    ) -> Summary:
        folded = fold(chunk, self._store, limit=self._ctx.excerpt_chars)
        notes = await self._ask(writer, summary, folded, calls)
        new = merge(summary, notes, folded)
        check(summary, new, folded, notes.retire)  # raises rather than lose a correction
        return new

    async def _ask(
        self, writer: str, summary: Summary | None, folded: Folded, calls: list[CallRecord]
    ) -> NotesParams:
        brain = self._router.brain(writer)
        prompt = [
            Message.system(SYSTEM),
            Message.user(
                f"{previous_notes(summary)}\n\nThe transcript of turns {folded.first + 1} to "
                f"{folded.last + 1}:\n\n{folded.text}"
            ),
        ]
        try:
            routed = await self._router.chat(
                "light",
                prompt,
                tools=[NOTES_TOOL],
                tool_choice="required" if brain.capabilities.forced_tool_choice else "auto",
                max_output_tokens=self._ctx.summary_tokens,
                override=writer,
                fallback=False,  # a summary never moves the conversation to another provider
                label="compact",
            )
        except (BrainError, AllBrainsFailedError) as exc:
            calls.extend(a for a in exc.attempts if isinstance(a, CallRecord))
            raise
        calls.extend(routed.attempts)
        call = next((c for c in routed.response.tool_calls if c.name == TOOL), None)
        if call is None:
            msg = "the model answered without writing the summary"
            raise SummaryError(msg)
        checked = validate_call(call, {TOOL: NotesParams})
        if isinstance(checked, ToolCallRejection):
            raise SummaryError(checked.reason)
        return cast("NotesParams", checked.args)

    def _shorten_paste(self, text: str) -> str:
        store = cast("OutputStore", self._store)
        return store.shorten(text, source="a long message the user pasted", untrusted=False)


def _plain_turn(messages: Sequence[Message]) -> list[Message]:
    return [m for m in (without_replay(m) for m in messages) if m is not None]


def _plain(exc: Exception) -> str:
    if isinstance(exc, _NothingToSummarizeError):
        return "there was nothing older to summarize"
    if isinstance(exc, SummaryError):
        return "the summary could not be written"
    return "the model could not be reached"


def _too_long(count: Count, budget: Budget, why: str) -> str:
    kind = "" if count.exact else "about "
    return (
        f"This is too long for the model: the conversation needs {kind}{count.tokens:,} tokens "
        f"and at most {budget.limit:,} fit, and {why}. Nothing was sent. Shorten your message, "
        "or start a new chat."
    )
