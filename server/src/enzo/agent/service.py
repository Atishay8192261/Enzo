"""Conversations end to end: ingest, compose, run the loop, persist (spec §3 steps 1-7).

A turn runs as a background task that outlives the HTTP request: if the client disconnects, the
agent still finishes and the turn is saved (§6.1, §9.2). Clients read events from a queue.
"""

import asyncio
import logging
import time
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal, Protocol

from enzo.agent.approvals import ApprovalRequest
from enzo.agent.history import close_open_calls, open_calls, repair
from enzo.agent.loop import AgentEvent, Progress, StopReason, TurnDone, TurnOutcome, run_turn
from enzo.agent.prompt import MemorySnippet, PastExcerpt, compose_prompt, fit_recall, render_past
from enzo.brain.models import Message, TextPart
from enzo.brain.router import CallRecord, Router, classify
from enzo.brain.tokens import TokenCounter
from enzo.compaction import Compaction, ContextWindow
from enzo.context import (
    BrowserInUse,
    conversation_var,
    new_turn_id,
    notice_var,
    request_var,
    said_var,
    timezone_var,
    turn_var,
)
from enzo.goals import Goals, goals_note
from enzo.goals.store import user_today
from enzo.memory import Fact, FileEdits, Memory
from enzo.privacy import Discretion, rules_note
from enzo.privacy.check import user_words
from enzo.semantic import SemanticIndex
from enzo.semantic.tools import where
from enzo.store.repository import ConversationInfo, Store
from enzo.store.schema import ApprovalRow
from enzo.tools.registry import ToolOutcome, ToolRegistry
from enzo.tools.stored import OutputStore

log = logging.getLogger("enzo.agent.service")

#: Past chats' turns that may ride with one message (each at most ``EXCERPT_CHARS``).
PAST_CHATS = 2

DEFAULT_SYSTEM_PROMPT = (
    resources.files("enzo.agent").joinpath("system_prompt.md").read_text(encoding="utf-8")
)


class ConversationNotFoundError(Exception):
    pass


class TurnInProgressError(Exception):
    """One turn at a time per conversation, so the transcript stays in order."""


@dataclass(frozen=True, slots=True)
class TurnMeta:
    """Metadata that arrives with a message (spec §3 step 1)."""

    timezone: str | None = None
    device: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: For the brain only, after the context: e.g. what a scheduled run is for.
    note: str | None = None


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """The turn crashed for a reason the loop could not turn into a stop reason."""

    message: str


class TurnHooks(Protocol):
    """Something that lives only as long as a chat's turn (a browser task, ADR 0027)."""

    async def turn_ended(self, conversation_id: uuid.UUID, stop: StopReason | None) -> None:
        """The turn ended (``stop``; None when it crashed). Runs before the client hears so."""

    async def moved_on(self, conversation_id: uuid.UUID) -> None:
        """A new message arrived: whatever the last turn left waiting is over."""

    async def stop(self, conversation_id: uuid.UUID) -> bool:
        """The user pressed Stop. True if something was stopped."""


type ServiceEvent = AgentEvent | TurnFailed | BrowserInUse
#: Who wrote a turn's new message: the user, a scheduled task's run (ADR 0022), a helper's brief
#: (the first message of a helper's own chat) or a handoff (helpers' reports, ADR 0028).
type Origin = Literal["user", "schedule", "brief", "handoff", "watch"]
_END = object()


@dataclass(frozen=True, slots=True)
class TurnPlan:
    """What a chat's next turn runs with, beyond the usual (a helper's chat, ADR 0028)."""

    #: For the brain only, after the context: e.g. reports that arrived for this chat.
    notes: tuple[str, ...] = ()
    #: Steps the turn may take; None: the service's own limit.
    max_steps: int | None = None
    #: Save after every step, so a restart picks the turn up from its last finished step.
    checkpoint: bool = False
    #: The reports this turn hands to the brain (helper ids), to mark as delivered once it ends.
    carrying: tuple[int, ...] = ()


class TurnPlanner(Protocol):
    """Shapes each turn of a chat and hears how it ended (helpers, ADR 0028)."""

    async def plan(self, conversation_id: uuid.UUID, *, message: bool) -> TurnPlan:
        """``message``: the turn starts with a new message (else it resumes a paused one)."""

    async def step_saved(self, conversation_id: uuid.UUID) -> None:
        """A planned turn saved a finished step (open apps may show it)."""

    async def turn_done(
        self, conversation_id: uuid.UUID, plan: TurnPlan, outcome: TurnOutcome | None
    ) -> None:
        """The turn is over and the chat is free again (``outcome`` None: it crashed)."""


@dataclass(slots=True)
class _Saved:
    """How much of a running turn is saved already (it is saved step by step when planned)."""

    messages: int
    calls: int = 0
    tools: int = 0
    compactions: int = 0
    #: The new message's position in the prompt, and who wrote it, until it is saved.
    marked: tuple[int, Origin] | None = None


class ConversationService:
    def __init__(  # noqa: PLR0913 - what a conversation draws on, all but three keyword-only
        self,
        store: Store,
        router: Router,
        registry: ToolRegistry,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        developer: str | None = None,
        memory: Memory | None = None,
        goals: Goals | None = None,
        index: SemanticIndex | None = None,
        max_steps: int = 16,
        outputs: OutputStore | None = None,
        hooks: Sequence[TurnHooks] = (),
        discretion: Discretion | None = None,
    ) -> None:
        self.store = store
        self.router = router
        self.registry = registry
        self.memory = memory
        self.goals = goals
        #: What is sent out is checked for private details; the user's rules (ADR 0029).
        self.discretion = discretion
        #: Past chats by meaning (spec §6.3): a few that a new message is about ride with it.
        self.index = index
        #: Long texts kept as files out of the context (pastes, older turns' long parts).
        self.outputs = outputs
        #: Learns each brain's real prompt sizes across chats (ADR 0024).
        self.counter = TokenCounter(router.config)
        #: Shapes turns and hears how they ended (helpers, ADR 0028); set once both exist.
        self.planner: TurnPlanner | None = None
        self._system = system_prompt
        #: Facts about this install the brain needs, such as where the user's Enzo folder is.
        self._developer = developer
        self._max_steps = max_steps
        self._hooks = tuple(hooks)
        self._running: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._stops: dict[uuid.UUID, asyncio.Event] = {}
        #: Chats a new turn is being set up for (claimed, not started yet).
        self._claimed: set[uuid.UUID] = set()
        #: Chats whose running turn Enzo started on its own (no one is waiting for its reply).
        self._background: set[uuid.UUID] = set()
        #: Chats where a user's message waits for such a turn to end.
        self._waiting: Counter[uuid.UUID] = Counter()

    def add_hook(self, hook: TurnHooks) -> None:
        self._hooks = (*self._hooks, hook)

    async def require(self, conversation_id: uuid.UUID) -> ConversationInfo:
        info = await self.store.conversation(conversation_id)
        if info is None:
            raise ConversationNotFoundError(str(conversation_id))
        return info

    async def send(
        self,
        conversation_id: uuid.UUID,
        text: str,
        meta: TurnMeta | None = None,
        *,
        origin: Origin = "user",
        background: bool = False,
    ) -> AsyncGenerator[ServiceEvent]:
        """Start a turn for a new user message; returns its event stream.

        If an approval is still open, the user has moved on: its calls are closed as "not run" and
        the open approval is dropped, so nothing runs unasked and the history stays valid (#39).

        ``background``: Enzo starts this turn on its own (a helper's work, a report arriving).
        The user's own message is never refused because of one: it waits for it to end, then
        goes (the chat stays free to talk, ADR 0028).
        """
        info = await self._claim(conversation_id, wait=origin == "user")
        try:
            return await self._send(info, text, meta, origin, background=background)
        finally:
            self._claimed.discard(conversation_id)

    async def _send(
        self,
        info: ConversationInfo,
        text: str,
        meta: TurnMeta | None,
        origin: Origin,
        *,
        background: bool,
    ) -> AsyncGenerator[ServiceEvent]:
        conversation_id = info.id
        for hook in self._hooks:
            await hook.moved_on(conversation_id)
        history = await self.store.history(conversation_id)
        closing = close_open_calls(history)
        if closing is not None:
            history.append(closing)
            dropped = await self.store.drop_pending(conversation_id)
            log.info("moved on", extra={"conversation": str(conversation_id), "dropped": dropped})
        user = _user_message(text, meta or TurnMeta())
        # Spec §3 step 2: what Enzo remembers that this message is about. (The chat is set for
        # this request too, so the recall's log line names it.)
        conversation_var.set(str(conversation_id))
        # Tools that deal in times (reminders) read the user's zone from the turn.
        timezone_var.set((meta or TurnMeta()).timezone)
        # A helper handed work in this turn gets the user's own words with its brief.
        request_var.set(text if origin in ("user", "schedule") else None)
        # The user may have edited Memory.md by hand: their edits count before anything is
        # recalled (spec §11.2, §11.3), and the brain hears what was not saved, to tell them.
        edits = await self.memory.read_edits() if self.memory else None
        recalled = await self.memory.recall(text) if self.memory else []
        past = await self._past_chats(text, conversation_id)
        memory, past = fit_recall(
            [_snippet(f) for f in recalled],
            past,
            self.router.config.context.recall_tokens,
        )
        notes = [edits_note(edits)] if edits else []
        if self.goals:  # active goals come with every message (spec §3 step 2, §6.4)
            notes += [n for n in (await self.goals.check_file(),) if n]
            block = goals_note(await self.goals.open(), user_today())
            notes += [block] if block else []
        notes += [render_past(past)] if past else []
        if self.discretion:  # the user's discretion rules come with every message (§12.5)
            rules = rules_note(await self.discretion.rules.applying(conversation_id))
            notes += [rules] if rules else []
        plan = await self._plan(conversation_id, message=True)
        notes += plan.notes
        prompt = compose_prompt(
            system=self._system,
            developer=self._developer,
            memory=memory,
            history=repair(history),
            user_message=user,
            notes=notes,
        )
        saved_from = len(prompt) - (2 if closing is not None else 1)
        # A message the user did not type is marked as such: the apps show a scheduled run or a
        # helper's brief for what it is. (The brain sees a user message; its note says who wrote
        # it.)
        marked: tuple[int, Origin] | None = (len(prompt) - 1, origin) if origin != "user" else None
        return self._start(
            info, prompt, first_new=saved_from, plan=plan, marked=marked, background=background
        )

    async def _past_chats(self, text: str, conversation_id: uuid.UUID) -> list[PastExcerpt]:
        """Turns of other chats this message is about, by meaning only (spec §6.3)."""
        if self.index is None or not self.index.by_meaning:
            return []
        hits = await self.index.find(
            text,
            sources={"turn"},
            limit=PAST_CHATS,
            mode="recall",
            chat=conversation_id,
            other_chats_only=True,
            by_meaning_only=True,
        )
        return [PastExcerpt(h.text, where(h)) for h in hits]

    async def resume(
        self, conversation_id: uuid.UUID, *, background: bool = False
    ) -> AsyncGenerator[ServiceEvent]:
        """Continue a paused turn (after an approval decision or an interruption)."""
        info = await self._claim(conversation_id, wait=False)
        try:
            history = repair(await self.store.history(conversation_id))
            prompt = compose_prompt(
                system=self._system,
                developer=self._developer,
                memory=[],
                history=history,
                user_message=None,
            )
            plan = await self._plan(conversation_id, message=False)
            return self._start(
                info, prompt, first_new=len(prompt), plan=plan, background=background
            )
        finally:
            self._claimed.discard(conversation_id)

    async def stop(self, conversation_id: uuid.UUID) -> bool:
        """Ask the running turn to stop (#46), and end what it left running (the browser).
        What already streamed is kept. False: nothing was running."""
        stop = self._stops.get(conversation_id)
        if stop is not None:
            stop.set()
        stopped = [await hook.stop(conversation_id) for hook in self._hooks]
        return stop is not None or any(stopped)

    async def cancel(self, conversation_id: uuid.UUID) -> None:
        """End a running turn on the spot, saving nothing more (the gateway is shutting down).
        What it saved step by step stays, so it can pick up from there after a restart."""
        task = self._running.get(conversation_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def running(self, conversation_id: uuid.UUID) -> bool:
        return conversation_id in self._running or conversation_id in self._claimed

    def busy(self, conversation_id: uuid.UUID) -> bool:
        """A turn runs, or the user's message waits to start one: Enzo starts nothing here."""
        return self.running(conversation_id) or self._waiting[conversation_id] > 0

    async def pending_approvals(self, conversation_id: uuid.UUID) -> list[ApprovalRow]:
        """Approvals waiting for the user right now: open ones for the calls the chat ends with.
        Older unanswered rows (from before #39 was fixed) are not shown."""
        history = await self.store.history(conversation_id)
        waiting = {c.id for c in open_calls(history)}
        rows = await self.store.pending_approvals(conversation_id)
        return [r for r in rows if r.call_id in waiting]

    async def wait_idle(self, conversation_id: uuid.UUID) -> None:
        task = self._running.get(conversation_id)
        if task is not None:
            await asyncio.shield(task)

    async def _claim(self, conversation_id: uuid.UUID, *, wait: bool) -> ConversationInfo:
        """Reserve the chat for one new turn, before anything else awaits: two messages sent at
        the same moment must never both start a turn (the second gets TurnInProgressError).
        ``wait``: a turn Enzo started on its own is waited for instead."""
        info = await self.require(conversation_id)
        while (
            wait
            and conversation_id in self._background
            and (task := self._running.get(conversation_id)) is not None
        ):
            self._waiting[conversation_id] += 1
            try:
                await asyncio.wait({task})
            finally:
                self._waiting[conversation_id] -= 1
        if self.running(conversation_id):
            raise TurnInProgressError(str(conversation_id))
        self._claimed.add(conversation_id)
        return info

    async def _plan(self, conversation_id: uuid.UUID, *, message: bool) -> TurnPlan:
        if self.planner is None:
            return TurnPlan()
        return await self.planner.plan(conversation_id, message=message)

    def _start(
        self,
        info: ConversationInfo,
        prompt: list[Message],
        *,
        first_new: int,
        plan: TurnPlan,
        marked: tuple[int, Origin] | None = None,
        background: bool = False,
    ) -> AsyncGenerator[ServiceEvent]:
        queue: asyncio.Queue[ServiceEvent | object] = asyncio.Queue()
        stop = asyncio.Event()
        saved = _Saved(messages=first_new, marked=marked)
        task = asyncio.create_task(self._drive(info, prompt, saved, queue, stop, plan))
        self._running[info.id] = task
        self._stops[info.id] = stop
        if background:
            self._background.add(info.id)
        task.add_done_callback(lambda done: self._release(info.id, done))
        return _drain(queue)

    def _release(self, conversation_id: uuid.UUID, task: "asyncio.Task[Any] | None") -> None:
        """The turn is over: the chat is free (a newer turn's entry is never touched)."""
        if task is not None and self._running.get(conversation_id) is task:
            self._running.pop(conversation_id, None)
            self._stops.pop(conversation_id, None)
            self._background.discard(conversation_id)

    async def _drive(
        self,
        info: ConversationInfo,
        prompt: list[Message],
        saved: _Saved,
        queue: asyncio.Queue[ServiceEvent | object],
        stop: asyncio.Event,
        plan: TurnPlan,
    ) -> None:
        # Every log line of this run, and its ledger rows, carry these (#48).
        turn_id = new_turn_id()
        conversation_var.set(str(info.id))
        turn_var.set(turn_id)
        # A discretion rule is saved only from the user's own words in this message (ADR 0029).
        said = list(user_words(prompt))
        said_var.set(said[-1] if said else None)
        # Tools tell the chat what they are doing outside it (the browser sign, ADR 0027).
        notice_var.set(queue.put_nowait)
        began = time.perf_counter()
        ended: StopReason | None = None
        done: TurnOutcome | None = None
        resume = saved.messages == len(prompt)
        try:
            decisions = await self.store.decisions(info.id)
            declined = await self.store.declined_calls(info.id)
            summaries = await self.store.compactions(info.id)
            window = ContextWindow(
                self.router,
                self.counter,
                record=summaries[-1] if summaries else None,
                store=self.outputs,
            )
            task, why = classify(self.router.config, prompt)
            log.info(
                "turn.start",
                extra={"task": task, "why": why, "messages": len(prompt), "resume": resume},
            )
            if plan.checkpoint and not resume:  # its message is kept before any work starts
                await self._save(info.id, saved, prompt, turn_id=turn_id)

            async def checkpoint(progress: Progress) -> None:
                """A step finished: keep it, so a restart goes on from here (ADR 0028)."""
                try:
                    await self._save(
                        info.id,
                        saved,
                        progress.messages,
                        calls=progress.brain_calls,
                        tools=progress.tool_outcomes,
                        compactions=window.made,
                        turn_id=turn_id,
                    )
                    if self.planner is not None:
                        await self.planner.step_saved(info.id)
                except Exception:  # the turn goes on; its end saves everything
                    log.exception("turn.checkpoint_failed")

            async for event in run_turn(
                self.router,
                self.registry,
                prompt,
                task=task,
                decisions=decisions,
                override=info.brain,
                affinity=str(info.id),
                max_steps=plan.max_steps or self._max_steps,
                max_output_tokens=self.router.config.context.reply_tokens,
                stop=stop,
                window=window,
                privacy=self.discretion,
                declined=declined,
                checkpoint=checkpoint if plan.checkpoint else None,
            ):
                if isinstance(event, TurnDone):
                    # Persist, and end what the turn had running, before telling the client.
                    o = event.outcome
                    await self._save(
                        info.id,
                        saved,
                        o.messages,
                        calls=o.brain_calls,
                        tools=o.tool_outcomes,
                        compactions=window.made,
                        pending=o.pending,
                        turn_id=turn_id,
                    )
                    _log_end(o, began)
                    ended, done = o.stop, o
                    await self._ended(info.id, ended)
                queue.put_nowait(event)
        except Exception as exc:  # report, never leave the client hanging
            log.exception("turn.failed", extra={"ms": round((time.perf_counter() - began) * 1000)})
            queue.put_nowait(TurnFailed(f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            if ended is None:  # crashed or cancelled: nothing it started may outlive it
                await self._ended(info.id, None)
            queue.put_nowait(_END)
            # The chat is free before the planner hears: it may start the next turn here.
            self._release(info.id, asyncio.current_task())
            if self.planner is not None:
                try:
                    await self.planner.turn_done(info.id, plan, done)
                except Exception:  # a planner must never break the turn that ended
                    log.exception("turn planner failed")

    async def _ended(self, conversation_id: uuid.UUID, stop: StopReason | None) -> None:
        for hook in self._hooks:
            try:
                await hook.turn_ended(conversation_id, stop)
            except Exception:  # a hook must never keep the turn from ending
                log.exception("turn hook failed")

    async def _save(  # noqa: PLR0913 - one save's parts, keyword-only
        self,
        conversation_id: uuid.UUID,
        saved: _Saved,
        messages: Sequence[Message],
        *,
        calls: Sequence[CallRecord] = (),
        tools: Sequence[ToolOutcome] = (),
        compactions: Sequence[Compaction] = (),
        pending: Sequence[ApprovalRequest] = (),
        turn_id: str,
    ) -> None:
        """Save what is new since this turn's last save (all of it, for most turns)."""
        marked = saved.marked
        await self.store.save_turn(
            conversation_id,
            messages=messages[saved.messages :],
            brain_calls=calls[saved.calls :],
            tool_outcomes=tools[saved.tools :],
            pending=pending,
            turn_id=turn_id,
            # Which saved message someone other than the user wrote (counted within this save).
            marked=None if marked is None else (marked[0] - saved.messages, marked[1]),
            compactions=compactions[saved.compactions :],
        )
        saved.messages, saved.calls, saved.tools = len(messages), len(calls), len(tools)
        saved.compactions, saved.marked = len(compactions), None
        if self.index is not None:  # the new turn becomes searchable in the background
            self.index.wake()


def _log_end(outcome: TurnOutcome, began: float) -> None:
    for request in outcome.pending:
        log.info("approval.wait", extra={"tool": request.tool, "approval": request.id})
    log.info(
        "turn.end",
        extra={
            "stop": outcome.stop,
            "steps": outcome.steps,
            "calls": len(outcome.brain_calls),
            "tools": len(outcome.tool_outcomes),
            "ms": round((time.perf_counter() - began) * 1000),
            **({"failure": outcome.failure} if outcome.failure else {}),
        },
    )


async def _drain(queue: "asyncio.Queue[ServiceEvent | object]") -> AsyncGenerator[ServiceEvent]:
    while (item := await queue.get()) is not _END:
        yield item  # type: ignore[misc]  # everything but the sentinel is a ServiceEvent


def edits_note(edits: FileEdits) -> str:
    """What the brain is told about the user's hand edits of Memory.md, to pass on."""
    counts = ((edits.changed, "changed"), (edits.added, "added"), (edits.removed, "removed"))
    done = [f"{n} {what}" for n, what in counts if n]
    note = "Memory.md was edited by hand since the last message"
    if done:
        note += f" ({', '.join(done)}): the memory that comes with this message is current."
    else:
        note += "."
    if edits.notes:
        notes = " ".join(n.removeprefix("> ") for n in edits.notes)
        note += (
            f" Enzo's notes on it: {notes} In your reply, tell the user plainly what was not "
            "saved and why."
        )
    return note


def _snippet(fact: Fact) -> MemorySnippet:
    if fact.edited_by_hand:
        who = "the user edited it by hand in Memory.md"
    else:
        who = "the user said it" if fact.said_by == "user" else "Enzo inferred it"
    where = f" in “{fact.chat}”" if fact.chat else ""
    text = f"{fact.about}: {fact.text}" if fact.about else fact.text
    return MemorySnippet(text, f"{who}{where}", fact.learned_at.date(), fact.id)


def _user_message(text: str, meta: TurnMeta) -> Message:
    """The message plus its context, kept in the user's turn so the cached prefix stays stable."""
    when = meta.received_at.isoformat(timespec="seconds")
    context = (
        f"[sent {when}; timezone: {meta.timezone or 'unknown'}; device: {meta.device or 'unknown'}]"
    )
    if meta.note:
        context += f" {meta.note}"
    return Message(role="user", parts=(TextPart(text=text), TextPart(text=context)))
