"""Conversations end to end: ingest, compose, run the loop, persist (spec §3 steps 1-7).

A turn runs as a background task that outlives the HTTP request: if the client disconnects, the
agent still finishes and the turn is saved (§6.1, §9.2). Clients read events from a queue.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Literal

from enzo.agent.history import close_open_calls, open_calls, repair
from enzo.agent.loop import AgentEvent, TurnDone, TurnOutcome, run_turn
from enzo.agent.prompt import MemorySnippet, compose_prompt
from enzo.brain.models import Message, TextPart
from enzo.brain.router import Router, classify
from enzo.brain.tokens import TokenCounter
from enzo.compaction import Compaction, ContextWindow
from enzo.context import conversation_var, new_turn_id, timezone_var, turn_var
from enzo.goals import Goals, goals_note
from enzo.goals.store import user_today
from enzo.memory import Fact, FileEdits, Memory
from enzo.store.repository import ConversationInfo, Store
from enzo.store.schema import ApprovalRow
from enzo.tools.registry import ToolRegistry
from enzo.tools.stored import OutputStore

log = logging.getLogger("enzo.agent.service")

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


type ServiceEvent = AgentEvent | TurnFailed
_END = object()


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
        max_steps: int = 16,
        outputs: OutputStore | None = None,
    ) -> None:
        self.store = store
        self.router = router
        self.registry = registry
        self.memory = memory
        self.goals = goals
        #: Long texts kept as files out of the context (pastes, older turns' long parts).
        self.outputs = outputs
        #: Learns each brain's real prompt sizes across chats (ADR 0024).
        self.counter = TokenCounter(router.config)
        self._system = system_prompt
        #: Facts about this install the brain needs, such as where the user's Enzo folder is.
        self._developer = developer
        self._max_steps = max_steps
        self._running: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._stops: dict[uuid.UUID, asyncio.Event] = {}

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
        origin: Literal["user", "schedule"] = "user",
    ) -> AsyncGenerator[ServiceEvent]:
        """Start a turn for a new user message; returns its event stream.

        If an approval is still open, the user has moved on: its calls are closed as "not run" and
        the open approval is dropped, so nothing runs unasked and the history stays valid (#39).
        """
        info = await self._claim(conversation_id)
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
        # The user may have edited Memory.md by hand: their edits count before anything is
        # recalled (spec §11.2, §11.3), and the brain hears what was not saved, to tell them.
        edits = await self.memory.read_edits() if self.memory else None
        recalled = await self.memory.recall(text) if self.memory else []
        notes = [edits_note(edits)] if edits else []
        if self.goals:  # active goals come with every message (spec §3 step 2, §6.4)
            notes += [n for n in (await self.goals.check_file(),) if n]
            block = goals_note(await self.goals.open(), user_today())
            notes += [block] if block else []
        prompt = compose_prompt(
            system=self._system,
            developer=self._developer,
            memory=[_snippet(f) for f in recalled],
            history=repair(history),
            user_message=user,
            notes=notes,
        )
        saved_from = len(prompt) - (2 if closing is not None else 1)
        # A scheduled run's message is marked as such: the apps show it as a run, not as words
        # the user typed. (The brain sees it as a user message, written by the schedule.)
        scheduled = saved_from + (1 if closing is not None else 0) if origin == "schedule" else None
        return self._start(info, prompt, first_new=saved_from, scheduled=scheduled)

    async def resume(self, conversation_id: uuid.UUID) -> AsyncGenerator[ServiceEvent]:
        """Continue a paused turn (after an approval decision or an interruption)."""
        info = await self._claim(conversation_id)
        history = repair(await self.store.history(conversation_id))
        prompt = compose_prompt(
            system=self._system,
            developer=self._developer,
            memory=[],
            history=history,
            user_message=None,
        )
        return self._start(info, prompt, first_new=len(prompt))

    def stop(self, conversation_id: uuid.UUID) -> bool:
        """Ask the running turn to stop (#46). What already streamed is kept. False: none runs."""
        stop = self._stops.get(conversation_id)
        if stop is None:
            return False
        stop.set()
        return True

    def running(self, conversation_id: uuid.UUID) -> bool:
        return conversation_id in self._running

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

    async def _claim(self, conversation_id: uuid.UUID) -> ConversationInfo:
        info = await self.require(conversation_id)
        if conversation_id in self._running:
            raise TurnInProgressError(str(conversation_id))
        return info

    def _start(
        self,
        info: ConversationInfo,
        prompt: list[Message],
        *,
        first_new: int,
        scheduled: int | None = None,
    ) -> AsyncGenerator[ServiceEvent]:
        queue: asyncio.Queue[ServiceEvent | object] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(self._drive(info, prompt, first_new, queue, stop, scheduled))
        self._running[info.id] = task
        self._stops[info.id] = stop

        def finished(_: asyncio.Task[None]) -> None:
            self._running.pop(info.id, None)
            self._stops.pop(info.id, None)

        task.add_done_callback(finished)
        return _drain(queue)

    async def _drive(
        self,
        info: ConversationInfo,
        prompt: list[Message],
        first_new: int,
        queue: asyncio.Queue[ServiceEvent | object],
        stop: asyncio.Event,
        scheduled: int | None = None,
    ) -> None:
        # Every log line of this run, and its ledger rows, carry these (#48).
        turn_id = new_turn_id()
        conversation_var.set(str(info.id))
        turn_var.set(turn_id)
        began = time.perf_counter()
        try:
            decisions = await self.store.decisions(info.id)
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
                extra={
                    "task": task,
                    "why": why,
                    "messages": len(prompt),
                    "resume": first_new == len(prompt),
                },
            )
            async for event in run_turn(
                self.router,
                self.registry,
                prompt,
                task=task,
                decisions=decisions,
                override=info.brain,
                affinity=str(info.id),
                max_steps=self._max_steps,
                max_output_tokens=self.router.config.context.reply_tokens,
                stop=stop,
                window=window,
            ):
                if isinstance(event, TurnDone):
                    # Persist before telling the client the turn is done.
                    await self._save(
                        info.id, event.outcome, first_new, turn_id, scheduled, window.made
                    )
                    _log_end(event.outcome, began)
                queue.put_nowait(event)
        except Exception as exc:  # report, never leave the client hanging
            log.exception("turn.failed", extra={"ms": round((time.perf_counter() - began) * 1000)})
            queue.put_nowait(TurnFailed(f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            queue.put_nowait(_END)

    async def _save(
        self,
        conversation_id: uuid.UUID,
        outcome: TurnOutcome,
        first_new: int,
        turn_id: str,
        scheduled: int | None = None,
        compactions: Sequence[Compaction] = (),
    ) -> None:
        await self.store.save_turn(
            conversation_id,
            messages=outcome.messages[first_new:],
            brain_calls=outcome.brain_calls,
            tool_outcomes=outcome.tool_outcomes,
            pending=outcome.pending,
            turn_id=turn_id,
            # Which saved message a scheduled run started (counted within this save).
            scheduled=None if scheduled is None else scheduled - first_new,
            compactions=compactions,
        )


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
