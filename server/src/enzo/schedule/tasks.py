"""Runs a scheduled task (spec §9.3): an agent turn in the task's own chat, then a report.

Each run is an ordinary turn, so every safety rule holds: tools validate, outside content stays
data, and an action that needs approval stops the run and waits for the user. The run's message
says what it is for (the brain sees it as the schedule's, not the user's, words); the apps show
it as a scheduled run. The report goes to the chat the task was set in, to open apps and to the
desktop, except for a watch ("tell me when…") whose condition is not met yet: that run is quiet.
"""

import asyncio
import logging
import re
import uuid

from enzo.agent.loop import TurnDone, TurnOutcome
from enzo.agent.service import ConversationService, TurnFailed, TurnInProgressError, TurnMeta
from enzo.context import job_var
from enzo.schedule.bus import Bus, Event
from enzo.schedule.notify import Notify, silent
from enzo.schedule.recurrence import zone
from enzo.schedule.store import Job, Outcome, Schedule, TaskEnd, TaskStart
from enzo.schedule.tools import FinishedTasks
from enzo.tools.registry import plain_words

log = logging.getLogger("enzo.schedule")

#: A task's chat busy with the user (they opened it and are talking): try again after this.
BUSY_RETRY_S = 60.0
BUSY_TRIES = 10
SUMMARY_CHARS = 200

_LEAD = re.compile(r"^\s*(?:[-*•>]+|#+|\d+[.)])\s+")
_MARKS = re.compile(r"\*\*|__|`")

#: Stops that mean the run did not do its work.
FAILED_STOPS = {"brain_failure", "refusal", "over_budget", "truncated", "max_steps", "cancelled"}


class TaskRunner:
    def __init__(
        self,
        schedule: Schedule,
        service: ConversationService,
        bus: Bus,
        finished: FinishedTasks,
        *,
        notify: Notify = silent,
        busy_retry_s: float = BUSY_RETRY_S,
    ) -> None:
        self._schedule = schedule
        self._service = service
        self._bus = bus
        #: Tasks whose stop condition a run found met (the finish_task tool), and why.
        self._finished = finished
        self._notify = notify
        self._busy_retry_s = busy_retry_s
        self._runs: set[asyncio.Task[TaskEnd]] = set()

    @property
    def running(self) -> int:
        return len(self._runs)

    def start(self, start: TaskStart) -> None:
        run = asyncio.get_running_loop().create_task(self.run(start))
        self._runs.add(run)
        run.add_done_callback(self._runs.discard)

    async def stop(self) -> None:
        for run in list(self._runs):
            run.cancel()
        await asyncio.gather(*self._runs, return_exceptions=True)

    async def run(self, start: TaskStart) -> TaskEnd:
        job = start.job
        chat: uuid.UUID | None = None
        try:
            chat = await self._chat_for(job)
            outcome, summary = await self._turn(job, chat, start)
        except Exception as exc:  # a run that breaks is recorded, never left "running"
            log.exception("task.run_failed", extra={"task": job.id})
            outcome, summary = "failed", f"The run broke ({type(exc).__name__}); see enzo logs."
        self._finished.take(job.id)  # a run that broke may have marked it: never carry it over
        end = await self._schedule.finish_run(
            job.id, due_at=start.due_at, late=start.late, outcome=outcome, summary=summary
        )
        await self._report(end, summary, chat)
        return end

    async def _chat_for(self, job: Job) -> uuid.UUID:
        """The task's own chat: made at its first run, named after what it does."""
        if job.chat_id is not None and await self._service.store.conversation(job.chat_id):
            return job.chat_id
        chat = await self._service.store.create_conversation(
            title=f"⏰ {plain_words(job.text, 56)}"
        )
        await self._schedule.set_chat(job.id, chat)
        return chat

    async def _turn(self, job: Job, chat: uuid.UUID, start: TaskStart) -> tuple[Outcome, str]:
        meta = TurnMeta(timezone=job.timezone, device="schedule", note=run_note(job, start))
        job_var.set(job.id)  # its tools (finish_task) know which task they serve
        for _ in range(BUSY_TRIES):
            try:
                events = await self._service.send(chat, job.text, meta, origin="schedule")
                break
            except TurnInProgressError:
                await asyncio.sleep(self._busy_retry_s)
        else:
            return "failed", "Its chat stayed busy, so this run did not happen."
        done: TurnOutcome | None = None
        async for event in events:
            if isinstance(event, TurnFailed):
                return "failed", f"The run failed: {plain_words(event.message, 120)}"
            if isinstance(event, TurnDone):
                done = event.outcome
        return judge(job, done, self._finished.take(job.id))

    async def _report(self, end: TaskEnd, summary: str, chat: uuid.UUID | None) -> None:
        job = end.job
        words = report_words(job, end, summary)
        self._bus.publish(
            Event(
                "task",
                {
                    "task": job.id,
                    "run": end.run_id,
                    "outcome": end.outcome,
                    "words": words,
                    "chat_id": str(chat) if chat else None,
                    "conversation_id": str(job.conversation_id) if job.conversation_id else None,
                    "stopped": end.stopped,
                },
            )
        )
        if end.outcome != "quiet":  # "not yet" is not worth interrupting anyone for
            await self._notify("Enzo: scheduled task", words)


def judge(job: Job, done: TurnOutcome | None, finished: str | None) -> tuple[Outcome, str]:
    """How a run went, from how its turn ended: and the line that reports it."""
    if done is None:
        return "failed", "The run ended without an answer."
    summary = first_line(done.text)
    if done.stop == "approval_pending":
        return "needs_approval", "It waits for your approval to go on."
    if done.stop in FAILED_STOPS:
        return "failed", summary or f"The run stopped ({done.stop})."
    if finished is not None:
        return "finished", summary or finished
    return ("quiet" if job.stop_when else "reported"), summary or "Done."


def run_note(job: Job, start: TaskStart) -> str:
    """What the brain is told about this run, after the message's context."""
    of = f" of at most {job.max_runs}" if job.max_runs and job.repeat != "once" else ""
    made = job.created_at.astimezone(zone(job.timezone)) if job.created_at else None
    parts = [
        f"Scheduled task #{job.id}, run {start.run}{of}"
        + (f", set by the user on {made:%a %-d %b}" if made else "")
        + ". The message above is the task: do it now, using tools as needed, and report the "
        "result in a few lines.",
    ]
    if job.stop_when:
        parts.append(
            f"The task is done when: {job.stop_when}. If that is true now, call finish_task "
            "and report it. If not, say in one line that it is not yet, and why."
        )
    if start.late:
        due = start.due_at.astimezone(zone(job.timezone))
        parts.append(f"This run is late: it was due {due:%a %H:%M} and Enzo was not running.")
    return " ".join(parts)


def report_words(job: Job, end: TaskEnd, summary: str) -> str:
    """The report in one line: what the task is, and what this run found."""
    what = plain_words(job.text, 60)
    tail = {
        "finished": " Done: it stops now.",
        "failed": " It stopped after failing several times." if end.stopped else "",
    }.get(end.outcome, " That was its last run." if end.stopped else "")
    return f"{what}: {summary}{tail}"


def first_line(text: str) -> str:
    """The report's first meaningful line, short: what the notification and notice show."""
    for raw in text.splitlines():
        # Plain words for a notification: no list or heading marks, no Markdown emphasis.
        line = _MARKS.sub("", _LEAD.sub("", raw)).strip()
        if line:
            return plain_words(line, SUMMARY_CHARS)
    return ""
