"""The people in the user's life (spec §6.4 tier 4, §6.5, §11.2, ADR 0036).

- **A record, not free text.** Each person has a number, a name, the other names they go by, and
  facts: their relation to the user, a birthday, what they like, what they eat, how they like to
  be contacted, notes, and ties to other people ("Priya is Sam's sister").
- **Evidence for every fact** (§6.5): who said it (the user, or Enzo inferred it), when, in which
  chat and run, and when it happened if known.
- **Corrections in place:** a correction is a new fact that names the one it replaces; the old
  one is kept, replaced, and never used again. A person has one relation and one birthday at a
  time, so a new one replaces the one before by itself.
- **Never secrets:** every word passes the memory guard before anything is written.
- **One truth with People.md** (§11.2): the user may edit the file by hand; Enzo reads the edits
  back before it reads or writes its records (cheap: size, time and inode first). A changed line
  corrects its fact, an added line is a new one, a removed line is forgotten, each as the user's
  own words, "edited by hand in People.md". The rules are those of Memory.md (ADR 0023).
- **Birthdays:** a yearly reminder can be set from one; it moves when the birthday is corrected
  and stops when the person is forgotten.
"""

import asyncio
import logging
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from enzo.context import current_conversation, turn_var
from enzo.memory.guard import secret_reason
from enzo.people.names import clashes, mentioned
from enzo.people.people_md import PEOPLE_FILE, FactLine, Parsed, Section, parse, render
from enzo.people.records import (
    HAND_EDIT,
    ORDER,
    SINGLE,
    Birthday,
    FactKind,
    Link,
    PeopleRefusedError,
    Person,
    PersonFact,
    SaidBy,
)
from enzo.schedule.recurrence import describe, next_after, zone
from enzo.store.schema import (
    Conversation,
    GoalRow,
    JobRow,
    PersonAliasRow,
    PersonFactRow,
    PersonLinkRow,
    PersonRow,
)

if TYPE_CHECKING:
    from enzo.semantic import SemanticIndex

log = logging.getLogger("enzo.people")

#: People that ride with one message at most (spec §3 step 2: only the top relevant).
RECALL = 3
#: Facts shown for one person in a message; the rest are a find_people call away.
FACTS_IN_PROMPT = 12
MAX_FACTS = 200
MAX_ALIASES = 12

MISSING = (
    "People.md was missing, so Enzo wrote it again and forgot no one. To make Enzo forget "
    "someone, delete their heading and lines, not the file."
)
UNREADABLE = (
    "People.md was not saved as plain text (UTF-8), so Enzo could not read the changes: it kept "
    "that copy as “{kept}” and wrote People.md again from its records."
)
KEPT = (
    "Some lines in People.md were not facts Enzo can read (a fact is a line starting with “- ” "
    "under someone's heading), so they were kept under “Not saved” in the file."
)


@dataclass(frozen=True, slots=True)
class NewFact:
    kind: FactKind
    text: str
    #: The fact it corrects: its number.
    replaces: int | None = None
    happened_on: date | None = None


@dataclass(frozen=True, slots=True)
class NewTie:
    #: The other person: their number, or a name or other name Enzo knows.
    other: int | str
    #: How this person relates to the other: "sister" in "Priya is Sam's sister".
    relation: str


@dataclass(frozen=True, slots=True)
class Noted:
    person: Person
    created: bool
    added: tuple[PersonFact, ...]
    #: Facts the change corrected: kept for the record, never used again.
    replaced: tuple[PersonFact, ...]
    #: Facts already known word for word: not kept twice.
    known: tuple[str, ...]
    #: Birthday reminders that moved to the corrected birthday.
    moved: tuple[Link, ...] = ()


@dataclass(frozen=True, slots=True)
class Forgotten:
    #: What was forgotten, in words: "Priya", "the fact “Likes: tea”", "the name “Pri”".
    what: str
    #: Birthday reminders that stopped with them.
    cancelled: tuple[int, ...] = ()


@dataclass(slots=True)
class FileEdits:
    """What reading back the user's hand edits of People.md did."""

    changed: int = 0
    added: int = 0
    removed: int = 0
    people_added: int = 0
    people_removed: int = 0
    renamed: int = 0
    names: int = 0
    #: In plain words, for the user: what was not saved and why (never a refused line's words).
    notes: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return any(
            (
                self.changed,
                self.added,
                self.removed,
                self.people_added,
                self.people_removed,
                self.renamed,
                self.names,
            )
        )


@dataclass(frozen=True, slots=True)
class _Seen:
    mtime_ns: int
    size: int
    inode: int


class People:
    def __init__(
        self,
        engine: AsyncEngine,
        workspace: Path | None = None,
        *,
        index: "SemanticIndex | None" = None,
    ) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._workspace = workspace
        #: Search by meaning (spec §6.3); without it, people are found by their names and words.
        self._index = index
        #: Reading edits back and writing the file never interleave (one gateway per database).
        self._lock = asyncio.Lock()
        self._seen: _Seen | None = None
        self._kept: list[str] = []
        self._notes: list[str] = []
        self._unread = False

    @property
    def file(self) -> Path | None:
        return self._workspace / PEOPLE_FILE if self._workspace else None

    # ---- writing ---------------------------------------------------------------------------

    async def note(  # noqa: PLR0913 - one person's changes, all keyword-only
        self,
        *,
        person: int | str | None = None,
        name: str | None = None,
        relation: str | None = None,
        aliases: Sequence[str] = (),
        birthday: Birthday | None = None,
        facts: Sequence[NewFact] = (),
        ties: Sequence[NewTie] = (),
        said_by: SaidBy = "user",
        today: date | None = None,
    ) -> Noted:
        """Add someone, or add to and correct what Enzo knows about them."""
        name = _words(name, "A name needs words.") if name is not None else None
        relation = _words(relation, "A relation needs words.") if relation is not None else None
        aliases = [_words(a, "A name needs words.") for a in aliases]
        facts = [replace_text(f, _words(f.text, "A fact needs words.")) for f in facts]
        ties = [NewTie(t.other, _words(t.relation, "A tie needs words.").lower()) for t in ties]
        async with self._lock:
            await self._read_back()
            async with self._sessions.begin() as session:
                done = await _Note(session, said_by, today).run(
                    person, name, relation, aliases, birthday, facts, ties
                )
            log.info(
                "people.saved",
                extra={
                    "person": done.person_id,
                    "created": done.created,
                    "added": len(done.added),
                    "replaced": len(done.replaced),
                },
            )
            self._changed()
            await self._write_file()
        found = await self.get(done.person_id)
        assert found is not None  # noqa: S101 - it was just written
        facts_now = {f.id: f for f in (*found.facts, *found.tied_by)}
        moved = tuple(link for link in found.links if link.id in done.moved)
        return Noted(
            person=found,
            created=done.created,
            added=tuple(facts_now[i] for i in done.added if i in facts_now),
            replaced=tuple(done.replaced),
            known=tuple(done.known),
            moved=moved,
        )

    async def forget(
        self, person: int | str, *, fact: int | None = None, alias: str | None = None
    ) -> Forgotten:
        """Forget one fact, one other name, or the person (with every fact about them)."""
        async with self._lock:
            await self._read_back()
            async with self._sessions.begin() as session:
                everyone = await _load(session)
                who = _resolve(person, everyone)
                if fact is not None:
                    found = next(
                        (f for f in (*who.facts, *who.tied_by) if f.id == fact),
                        None,
                    )
                    if found is None:
                        msg = f"#{fact} is not a current fact about {who.name}."
                        raise PeopleRefusedError(msg)
                    await _forget_fact(session, fact)
                    gone = Forgotten(f"the fact “{found.line()}” about {who.name}")
                elif alias is not None:
                    row = next(
                        (
                            r
                            for r in await session.scalars(
                                select(PersonAliasRow).where(PersonAliasRow.person_id == who.id)
                            )
                            if r.alias.casefold() == alias.strip().casefold()
                        ),
                        None,
                    )
                    if row is None:
                        msg = f"{who.name} is not called “{alias}” in Enzo's records."
                        raise PeopleRefusedError(msg)
                    await session.delete(row)
                    gone = Forgotten(f"the name “{row.alias}” for {who.name}")
                else:
                    cancelled = await _forget_person(session, who.id)
                    gone = Forgotten(who.name, cancelled)
            log.info(
                "people.forgotten",
                extra={"person": who.id, "fact": fact, "alias": alias is not None},
            )
            self._changed()
            await self._write_file()
        return gone

    async def remind_birthday(
        self,
        person: int | str,
        *,
        at: time,
        days_before: int,
        timezone: str,
        now: datetime | None = None,
    ) -> Link:
        """A yearly reminder of someone's birthday (or ``days_before`` it), at ``at`` local."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            everyone = await _load(session)
            who = _resolve(person, everyone)
            born = who.birthday
            if born is None:
                msg = (
                    f"Enzo does not know {who.name}'s birthday yet: ask the user, note it, then "
                    "set the reminder."
                )
                raise PeopleRefusedError(msg)
            if any(link.days_before == days_before for link in who.links):
                msg = f"There is already a reminder of {who.name}'s birthday for that day."
                raise PeopleRefusedError(msg)
            first, local = _birthday_times(born, at, days_before, timezone, now)
            row = JobRow(
                kind="reminder",
                text=_birthday_words(who.name, born, days_before),
                timezone=timezone,
                first_at=local,
                repeat="yearly",
                every=1,
                next_run_at=first,
                status="active",
                conversation_id=current_conversation(),
                turn_id=turn_var.get(),
            )
            session.add(row)
            await session.flush()
            session.add(PersonLinkRow(person_id=who.id, job_id=row.id, days_before=days_before))
            made = row.id
        log.info("reminder.set", extra={"reminder": made, "repeat": "yearly", "birthday": who.id})
        found = await self.get(who.id)
        assert found is not None  # noqa: S101 - it was just read
        return next(link for link in found.links if link.id == made)

    # ---- reading ---------------------------------------------------------------------------

    async def all(self) -> list[Person]:
        """Everyone Enzo knows, by name."""
        await self.read_edits()
        async with self._sessions() as session:
            return await _load(session)

    async def get(self, person_id: int) -> Person | None:
        async with self._sessions() as session:
            found = await _load(session, only=person_id)
        return found[0] if found else None

    async def resolve(self, ref: int | str) -> Person:
        """Someone by number, or by a name or other name only they go by."""
        return _resolve(ref, await self.all())

    async def find(self, query: str | None, limit: int = 10) -> list[Person]:
        """People ``query`` names or is about: by name, relation and words, then by meaning."""
        everyone = await self.all()
        if not query or not query.strip():
            return everyone[:limit]
        out = {p.id: p for p in mentioned(query, everyone)}
        words = _content_words(query)
        for p in everyone:
            text = " ".join(
                [*p.names, p.relation or "", *(f.text for f in p.facts)]
            ).casefold()
            if words and all(w in text for w in words):
                out.setdefault(p.id, p)
        for p in await self._by_meaning(query, everyone, "search"):
            out.setdefault(p.id, p)
        return list(out.values())[:limit]

    async def recall(self, message: str) -> list[Person]:
        """Who a new message is about: named (or called by relation), then by meaning."""
        everyone = await self.all()
        if not everyone:
            return []
        found = {p.id: p for p in mentioned(message, everyone)}
        for p in await self._by_meaning(message, everyone, "recall"):
            found.setdefault(p.id, p)
        people = list(found.values())[:RECALL]
        if people:
            log.info("people.recalled", extra={"people": [p.id for p in people]})
        return people

    async def _by_meaning(
        self, query: str, everyone: Sequence[Person], mode: Literal["recall", "search"]
    ) -> list[Person]:
        if self._index is None or not self._index.by_meaning:
            return []
        hits = await self._index.find(query, sources={"person"}, limit=RECALL, mode=mode)
        by_id = {p.id: p for p in everyone}
        return [by_id[h.person_id] for h in hits if h.person_id in by_id]

    # ---- People.md -------------------------------------------------------------------------

    async def read_edits(self) -> FileEdits | None:
        """Read back what the user changed in People.md since Enzo last looked; None when there
        is nothing to tell. Cheap when the file is as Enzo left it."""
        if self.file is None:
            return None
        async with self._lock:
            return await self._read_back()

    async def _read_back(self) -> FileEdits | None:
        path = self.file
        if path is None:
            return None
        try:
            return await self._reconcile(path)
        except Exception:  # a broken read never costs the user their edit, nor the turn
            log.exception("people.file_unread")
            self._unread = True
            return None

    async def _reconcile(self, path: Path) -> FileEdits | None:
        stat = await asyncio.to_thread(_stat, path)
        if stat is not None and stat == self._seen:
            return None
        if stat is None:  # never written, or deleted by hand: write it again, forget no one
            self._unread = False
            async with self._sessions() as session:
                anyone = await session.scalar(select(PersonRow.id).limit(1))
            if anyone is None:
                self._seen = None
                return None
            await self._write_file()
            return FileEdits(notes=[MISSING])
        try:
            body = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            kept = await asyncio.to_thread(_set_aside, path)
            self._unread = False
            await self._write_file()
            return FileEdits(notes=[UNREADABLE.format(kept=kept)])
        parsed = parse(body)
        saved_at = datetime.fromtimestamp(stat.mtime_ns / 1e9, UTC)
        async with self._sessions.begin() as session:
            edits = await _Edit(session, parsed, saved_at).run()
        self._unread = False
        self._kept, kept_notes = _keepable(parsed.kept)
        file_notes = list(edits.notes)
        self._notes = [n for n in parsed.notes if not secret_reason(n)] + file_notes + kept_notes
        edits.notes = file_notes + kept_notes
        if self._kept and parsed.moved:
            edits.notes.append(KEPT)
        if edits.any:
            log.info(
                "people.file_edits",
                extra={
                    "changed": edits.changed,
                    "added": edits.added,
                    "removed": edits.removed,
                    "people_added": edits.people_added,
                    "people_removed": edits.people_removed,
                },
            )
            self._changed()
        if edits.any or edits.notes:
            await self._write_file()
            return edits
        self._seen = stat  # only the layout changed: the user's file stays as they left it
        return None

    def _changed(self) -> None:
        if self._index is not None:
            self._index.wake()

    async def _write_file(self) -> None:
        path = self.file
        if path is None or self._unread:
            return
        async with self._sessions() as session:
            everyone = await _load(session)
        body = render(everyone, written_at=datetime.now(UTC), kept=self._kept, notes=self._notes)
        self._seen = await asyncio.to_thread(_write_whole, path, body)


def replace_text(fact: NewFact, text: str) -> NewFact:
    return NewFact(fact.kind, text, fact.replaces, fact.happened_on)


# ---- loading ---------------------------------------------------------------------------------


async def _load(session: AsyncSession, *, only: int | None = None) -> list[Person]:
    """People as records: names, current facts (ties both ways) and links, by name."""
    rows = list(
        await session.execute(
            select(PersonRow, Conversation.title).outerjoin(
                Conversation, Conversation.id == PersonRow.conversation_id
            )
        )
    )
    names = {p.id: p.name for p, _t in rows}
    aliases: dict[int, list[str]] = {}
    for a in await session.scalars(select(PersonAliasRow).order_by(PersonAliasRow.id)):
        aliases.setdefault(a.person_id, []).append(a.alias)
    facts: dict[int, list[PersonFact]] = {}
    tied_by: dict[int, list[PersonFact]] = {}
    for f, title in await session.execute(
        select(PersonFactRow, Conversation.title)
        .outerjoin(Conversation, Conversation.id == PersonFactRow.conversation_id)
        .where(PersonFactRow.status == "active")
        .order_by(PersonFactRow.created_at, PersonFactRow.id)
    ):
        fact = _fact(f, title, names)
        facts.setdefault(f.person_id, []).append(fact)
        if f.other_id is not None:
            tied_by.setdefault(f.other_id, []).append(fact)
    links = await _links(session, only)
    out = [
        Person(
            id=p.id,
            name=p.name,
            aliases=tuple(aliases.get(p.id, [])),
            facts=tuple(sorted(facts.get(p.id, []), key=lambda f: ORDER.index(f.kind))),
            tied_by=tuple(tied_by.get(p.id, [])),
            created_at=p.created_at,
            chat=title,
            conversation_id=p.conversation_id,
            edited_by_hand=p.turn_id == HAND_EDIT,
            links=tuple(links.get(p.id, [])),
        )
        for p, title in rows
        if only is None or p.id == only
    ]
    return sorted(out, key=lambda p: (p.name.casefold(), p.id))


async def _links(session: AsyncSession, only: int | None) -> dict[int, list[Link]]:
    out: dict[int, list[Link]] = {}
    query = (
        select(PersonLinkRow, GoalRow, JobRow)
        .outerjoin(GoalRow, GoalRow.id == PersonLinkRow.goal_id)
        .outerjoin(JobRow, JobRow.id == PersonLinkRow.job_id)
        .order_by(PersonLinkRow.id)
    )
    if only is not None:
        query = query.where(PersonLinkRow.person_id == only)
    for link, goal, job in await session.execute(query):
        if goal is not None:
            out.setdefault(link.person_id, []).append(
                Link("goal", goal.id, goal.title, goal.status)
            )
        elif job is not None and job.status == "active":
            tz = zone(job.timezone)
            first = job.first_at.astimezone(tz)
            nxt = job.next_run_at.astimezone(tz) if job.next_run_at else None
            out.setdefault(link.person_id, []).append(
                Link(
                    "reminder" if job.kind == "reminder" else "task",
                    job.id,
                    job.text,
                    describe(first, job.repeat, job.every),  # type: ignore[arg-type]
                    f"{nxt:%a %-d %b %H:%M}" if nxt else None,
                    link.days_before,
                )
            )
    return out


def _fact(row: PersonFactRow, chat: str | None, names: dict[int, str]) -> PersonFact:
    birthday = (
        Birthday(row.month, row.day, row.year)
        if row.kind == "birthday" and row.month and row.day
        else None
    )
    return PersonFact(
        id=row.id,
        person_id=row.person_id,
        kind=row.kind,  # type: ignore[arg-type]  # the table's check keeps it a FactKind
        text=row.text,
        said_by=row.said_by,  # type: ignore[arg-type]
        happened_on=row.happened_on,
        learned_at=row.created_at,
        chat=chat,
        conversation_id=row.conversation_id,
        edited_by_hand=row.turn_id == HAND_EDIT,
        other_id=row.other_id,
        other_name=names.get(row.other_id) if row.other_id is not None else None,
        holder_name=names.get(row.person_id),
        birthday=birthday,
    )


def _resolve(ref: int | str, everyone: Sequence[Person]) -> Person:
    if isinstance(ref, int):
        found = next((p for p in everyone if p.id == ref), None)
        if found is None:
            msg = f"There is no person #{ref}: find them first."
            raise PeopleRefusedError(msg)
        return found
    key = " ".join(ref.split()).casefold()
    matches = [p for p in everyone if any(n.casefold() == key for n in p.names)]
    if not matches:
        msg = f"Enzo knows no one called “{ref}”: find them first, or add them."
        raise PeopleRefusedError(msg)
    if len(matches) > 1:
        numbers = ", ".join(f"#{p.id}" for p in matches)
        msg = f"More than one person is called “{ref}” ({numbers}): use their number."
        raise PeopleRefusedError(msg)
    return matches[0]


# ---- one change from the brain ----------------------------------------------------------------


@dataclass(slots=True)
class _Done:
    person_id: int = 0
    created: bool = False
    added: list[int] = field(default_factory=list)
    replaced: list[PersonFact] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    moved: list[int] = field(default_factory=list)


class _Note:
    """One call's changes, in one transaction: all of it or none."""

    def __init__(self, session: AsyncSession, said_by: SaidBy, today: date | None) -> None:
        self.session = session
        self.said_by = said_by
        self.today = today
        self.done = _Done()

    async def run(  # noqa: PLR0913 - the parts of one change
        self,
        person: int | str | None,
        name: str | None,
        relation: str | None,
        aliases: Sequence[str],
        birthday: Birthday | None,
        facts: Sequence[NewFact],
        ties: Sequence[NewTie],
    ) -> _Done:
        everyone = await _load(self.session)
        row = await self._person(person, name, everyone)
        self.done.person_id = row.id
        await self._aliases(row, aliases, everyone)
        if relation is not None:
            await self._single(row.id, NewFact("relation", relation), everyone)
        if birthday is not None:
            changed = await self._single(
                row.id, NewFact("birthday", birthday.words()), everyone, birthday
            )
            if changed:
                self.done.moved = await _move_birthday(self.session, row.id, row.name, birthday)
        for fact in facts:
            await self._fact(row.id, fact, everyone)
        for tie in ties:
            await self._tie(row, tie, everyone)
        count = sum(len(p.facts) for p in everyone if p.id == row.id) + len(self.done.added)
        if count > MAX_FACTS:
            msg = f"At most {MAX_FACTS} facts about one person: keep the ones that matter."
            raise PeopleRefusedError(msg)
        return self.done

    async def _person(
        self, person: int | str | None, name: str | None, everyone: Sequence[Person]
    ) -> PersonRow:
        if person is None:
            if not name:
                msg = "Give the person's name, or their number if Enzo knows them already."
                raise PeopleRefusedError(msg)
            clash = clashes(name, everyone)
            if clash is not None:
                about = f", {clash.relation}" if clash.relation else ""
                msg = (
                    f"Enzo already knows someone called {name}: #{clash.id}{about}. If it is the "
                    f"same person, pass person={clash.id}; if it is someone else, give a fuller "
                    "name to tell them apart."
                )
                raise PeopleRefusedError(msg)
            row = PersonRow(name=name, conversation_id=current_conversation(), turn_id=turn_var.get())
            self.session.add(row)
            await self.session.flush()
            self.done.created = True
            return row
        who = _resolve(person, everyone)
        found = await self.session.get(PersonRow, who.id, with_for_update=True)
        assert found is not None  # noqa: S101 - it was just loaded
        if name and name != found.name:
            clash = clashes(name, everyone, but=found.id)
            if clash is not None:
                msg = f"#{clash.id} is already called {name}: use another name."
                raise PeopleRefusedError(msg)
            found.name, found.named_at = name, datetime.now(UTC)
        return found

    async def _aliases(self, row: PersonRow, aliases: Sequence[str], people: Sequence[Person]) -> None:
        now = [p for p in people if p.id == row.id]
        have = {n.casefold() for p in now for n in p.names} | {row.name.casefold()}
        for alias in aliases:
            if alias.casefold() in have:
                continue
            clash = clashes(alias, people, but=row.id)
            if clash is not None:
                msg = f"“{alias}” is already what Enzo calls #{clash.id} ({clash.name})."
                raise PeopleRefusedError(msg)
            if len(have) > MAX_ALIASES:
                msg = f"At most {MAX_ALIASES} other names for one person."
                raise PeopleRefusedError(msg)
            have.add(alias.casefold())
            self.session.add(
                PersonAliasRow(
                    person_id=row.id,
                    alias=alias,
                    conversation_id=current_conversation(),
                    turn_id=turn_var.get(),
                )
            )

    async def _single(
        self,
        person_id: int,
        fact: NewFact,
        people: Sequence[Person],
        birthday: Birthday | None = None,
    ) -> bool:
        """Set the one relation or birthday: the current one, if different, is replaced."""
        current = next(
            (f for p in people if p.id == person_id for f in p.facts if f.kind == fact.kind),
            None,
        )
        if current is not None and current.text.casefold() == fact.text.casefold():
            self.done.known.append(current.line())
            return False
        if current is not None:
            await self._retire(current)
        self._add(person_id, fact, replaces=current.id if current else None, birthday=birthday)
        await self.session.flush()
        return True

    async def _fact(self, person_id: int, fact: NewFact, people: Sequence[Person]) -> None:
        mine = [f for p in people if p.id == person_id for f in p.facts]
        if fact.kind in SINGLE:
            await self._single(person_id, fact, people)
            return
        old = None
        if fact.replaces is not None:
            old = next((f for f in mine if f.id == fact.replaces and f.kind != "tie"), None)
            if old is None:
                msg = (
                    f"#{fact.replaces} is not a current fact about this person: find_people "
                    "shows their facts with their numbers."
                )
                raise PeopleRefusedError(msg)
        same = next(
            (
                f
                for f in mine
                if f.kind == fact.kind and f.text.casefold() == fact.text.casefold() and f != old
            ),
            None,
        )
        if same is not None:
            self.done.known.append(same.line())
            if old is not None:  # the correction says what another line already says
                await self._retire(old)
                await _forget_fact(self.session, old.id)
            return
        if old is not None:
            await self._retire(old)
        self._add(person_id, fact, replaces=old.id if old else None)
        await self.session.flush()

    async def _tie(self, row: PersonRow, tie: NewTie, people: Sequence[Person]) -> None:
        other = _resolve(tie.other, people)
        if other.id == row.id:
            msg = "A tie is between two different people."
            raise PeopleRefusedError(msg)
        current = next(
            (
                f
                for p in people
                if p.id == row.id
                for f in p.facts
                if f.kind == "tie" and f.other_id == other.id
            ),
            None,
        )
        if current is not None and current.text == tie.relation:
            self.done.known.append(current.line())
            return
        if current is not None:
            await self._retire(current)
        self._add(
            row.id, NewFact("tie", tie.relation), replaces=current.id if current else None,
            other=other.id,
        )
        await self.session.flush()

    async def _retire(self, fact: PersonFact) -> None:
        old = await self.session.get(PersonFactRow, fact.id, with_for_update=True)
        assert old is not None  # noqa: S101 - it was just loaded
        old.status = "replaced"
        await self.session.flush()  # before its successor: one current relation, birthday, tie
        self.done.replaced.append(fact)

    def _add(
        self,
        person_id: int,
        fact: NewFact,
        *,
        replaces: int | None,
        birthday: Birthday | None = None,
        other: int | None = None,
    ) -> None:
        row = PersonFactRow(
            person_id=person_id,
            kind=fact.kind,
            text=fact.text,
            other_id=other,
            month=birthday.month if birthday else None,
            day=birthday.day if birthday else None,
            year=birthday.year if birthday else None,
            said_by=self.said_by,
            happened_on=fact.happened_on,
            replaces=replaces,
            conversation_id=current_conversation(),
            turn_id=turn_var.get(),
        )
        self.session.add(row)
        self._pending.append(row)

    @property
    def _pending(self) -> list[PersonFactRow]:
        return self._rows

    async def flushed(self) -> None:  # pragma: no cover - kept for symmetry
        await self.session.flush()

    _rows: list[PersonFactRow]

    def __init_subclass__(cls) -> None:  # pragma: no cover
        super().__init_subclass__()


async def _forget_fact(session: AsyncSession, fact_id: int) -> int:
    """Delete a fact and every version before it; how many rows went."""
    chain = [fact_id]
    row = await session.get(PersonFactRow, fact_id)
    while row is not None and row.replaces is not None:
        chain.append(row.replaces)
        row = await session.get(PersonFactRow, row.replaces)
    await session.execute(delete(PersonFactRow).where(PersonFactRow.id.in_(chain)))
    return len(chain)


async def _forget_person(session: AsyncSession, person_id: int) -> tuple[int, ...]:
    """Delete someone and all Enzo knows about them; their birthday reminders stop."""
    birthday_jobs = list(
        await session.scalars(
            select(JobRow)
            .join(PersonLinkRow, PersonLinkRow.job_id == JobRow.id)
            .where(
                PersonLinkRow.person_id == person_id,
                PersonLinkRow.days_before.is_not(None),
                JobRow.status == "active",
            )
            .with_for_update()
        )
    )
    for job in birthday_jobs:
        job.status, job.next_run_at = "cancelled", None
    await session.execute(delete(PersonRow).where(PersonRow.id == person_id))
    for job in birthday_jobs:
        log.info("reminder.cancelled", extra={"reminder": job.id, "person": person_id})
    return tuple(j.id for j in birthday_jobs)


def _birthday_times(
    born: Birthday, at: time, days_before: int, timezone: str, now: datetime
) -> tuple[datetime, datetime]:
    """The first time a birthday reminder comes, and its anchor (a past year's date, so a 29
    February birthday is the 29th in leap years and the 28th in others)."""
    tz = zone(timezone)
    year = now.astimezone(tz).year
    if (born.month, born.day) == (2, 29):
        while not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
            year -= 1
    day = born.on(year) - timedelta(days=days_before)
    local = datetime.combine(day, at.replace(tzinfo=None)).replace(tzinfo=tz)
    first = next_after(local, "yearly", 1, now - timedelta(minutes=1))
    assert first is not None  # noqa: S101 - a yearly repeat always comes again
    return first, local


def _birthday_words(name: str, born: Birthday, days_before: int) -> str:
    day = f"{born.day} {born.on(2000):%B}"
    if days_before == 0:
        return f"It's {name}'s birthday today ({day})"
    when = "tomorrow" if days_before == 1 else f"in {days_before} days"
    return f"{name}'s birthday is {when} ({day})"


async def _move_birthday(
    session: AsyncSession, person_id: int, name: str, born: Birthday
) -> list[int]:
    """The birthday changed: its yearly reminders move to the new date, at the same time."""
    moved: list[int] = []
    now = datetime.now(UTC)
    for link, job in await session.execute(
        select(PersonLinkRow, JobRow)
        .join(JobRow, JobRow.id == PersonLinkRow.job_id)
        .where(
            PersonLinkRow.person_id == person_id,
            PersonLinkRow.days_before.is_not(None),
            JobRow.status == "active",
        )
        .with_for_update(of=JobRow)
    ):
        at = job.first_at.astimezone(zone(job.timezone)).timetz()
        first, local = _birthday_times(born, at, link.days_before or 0, job.timezone, now)
        job.first_at, job.next_run_at = local, first
        job.text = _birthday_words(name, born, link.days_before or 0)
        moved.append(job.id)
        log.info("reminder.moved", extra={"reminder": job.id, "person": person_id})
    return moved


# ---- hand edits of People.md ------------------------------------------------------------------


class _Edit:
    """Apply a hand edit of People.md to the records, in one transaction: all of it or nothing.

    Which wins when the file and the records both changed since Enzo wrote the copy the user
    edited (ADR 0023, ADR 0036): a line the user did not change never undoes a change made in
    chat; a line they did change wins when it was saved after the chat's change; a fact or a
    person forgotten in chat stays forgotten; nothing learned after that copy is "removed".
    """

    def __init__(self, session: AsyncSession, parsed: Parsed, saved_at: datetime) -> None:
        self.session = session
        self.parsed = parsed
        self.saved_at = saved_at
        self.written_at = parsed.written_at
        self.edits = FileEdits()
        self._day = f"> {datetime.now(UTC):%-d %b %Y}:"
        self.people: dict[int, PersonRow] = {}
        self.facts: dict[int, PersonFactRow] = {}
        self.after: dict[int, PersonFactRow] = {}
        self.aliases: list[PersonAliasRow] = []
        #: Which sections stand for which person.
        self.sections: dict[int, list[Section]] = {}
        #: Current facts some line stands for.
        self.stands: set[int] = set()
        #: (the fact a line changes, or None for a new one; whose line; the line).
        self.changes: list[tuple[PersonFactRow | None, int, FactLine]] = []
        self.missing_once = False

    def note(self, words: str) -> None:
        self.edits.notes.append(f"{self._day} {words}")

    async def run(self) -> FileEdits:
        s = self.session
        self.people = {r.id: r for r in await s.scalars(select(PersonRow).with_for_update())}
        self.facts = {r.id: r for r in await s.scalars(select(PersonFactRow).with_for_update())}
        self.after = {r.replaces: r for r in self.facts.values() if r.replaces is not None}
        self.aliases = list(await s.scalars(select(PersonAliasRow).with_for_update()))
        await self._assign()
        for person_id, sections in self.sections.items():
            await self._names(person_id, sections)
            for section in sections:
                for line in section.lines:
                    self._line(person_id, line)
        for number in self.parsed.emptied:
            head = self.current(number)
            if head is not None:
                self.stands.add(head.id)
                self.note(
                    f"the line for #{number} had no words left, so Enzo kept that fact as it "
                    "was. To make Enzo forget it, delete the whole line."
                )
        for number in self.parsed.unreadable:
            head = self.current(number)
            if head is not None:
                self.stands.add(head.id)
                self.note(
                    f"a birthday line (#{number}) was not a date Enzo can read, so the birthday "
                    "stays as it was. Write it like 14 March or 1990-03-14."
                )
        await self._apply_changes()
        await self._removed()
        await s.flush()
        return self.edits

    def current(self, fact_id: int | None) -> PersonFactRow | None:
        """The current version of a fact: its later corrections followed to the newest."""
        row = self.facts.get(fact_id) if fact_id is not None else None
        while row is not None and row.status != "active":
            row = self.after.get(row.id)
        return row

    def _names(self, person_id: int) -> set[str]:  # type: ignore[override]
        raise NotImplementedError  # pragma: no cover - replaced below

    # -- who each heading is

    async def _assign(self) -> None:
        for section in self.parsed.sections:
            if section.id is not None:
                if section.id in self.people:
                    self.sections.setdefault(section.id, []).append(section)
                else:
                    self.note(
                        f"the heading “{section.name}” is numbered #{section.id}, for someone "
                        "you asked Enzo to forget, so its lines were left out. To add them again, "
                        "remove the number from the heading."
                    )
                continue
            key = section.name.casefold()
            matches = [
                pid
                for pid, row in self.people.items()
                if row.name.casefold() == key
                or any(a.person_id == pid and a.alias.casefold() == key for a in self.aliases)
            ]
            if len(matches) == 1:
                self.sections.setdefault(matches[0], []).append(section)
            elif matches:
                self.note(
                    f"more than one person is called “{section.name}”, so the lines under that "
                    "heading were not saved. Put them under the right numbered heading."
                )
            else:
                await self._new_person(section)

    async def _new_person(self, section: Section) -> None:
        reason = secret_reason(section.name)
        if reason:
            self.note(f"a heading you added was not saved: {reason}, and Enzo never keeps those.")
            return
        row = PersonRow(name=section.name, turn_id=HAND_EDIT)
        self.session.add(row)
        await self.session.flush()
        self.people[row.id] = row
        self.sections[row.id] = [section]
        self.edits.people_added += 1

    # -- names

    async def _names(self, person_id: int, sections: list[Section]) -> None:  # type: ignore[no-redef]
        row = self.people[person_id]
        numbered = next((s for s in sections if s.id is not None), sections[0])
        wanted = numbered.name
        renamed_since = self.written_at is not None and row.named_at > self.written_at
        if wanted and wanted != row.name and not renamed_since and row.turn_id != "__new__":
            reason = secret_reason(wanted)
            clash = self._clash(wanted, person_id)
            if reason:
                self.note(f"a new name for #{person_id} was not saved: {reason}.")
            elif clash is not None:
                self.note(
                    f"#{person_id} was not renamed “{wanted}”: that is already what Enzo calls "
                    f"#{clash}."
                )
            else:
                row.name, row.named_at = wanted, datetime.now(UTC)
                self.edits.renamed += 1
        listed = [s.aliases for s in sections if s.aliases is not None]
        file_names = [n for names in listed for n in names]
        have = [a for a in self.aliases if a.person_id == person_id]
        keys = {n.casefold() for n in file_names}
        for alias in have:
            if alias.alias.casefold() in keys:
                continue
            if self.written_at is None:
                self._no_stamp()
                continue
            if alias.created_at <= self.written_at:
                await self.session.delete(alias)
                self.aliases.remove(alias)
                self.edits.names += 1
        known = {a.alias.casefold() for a in self.aliases if a.person_id == person_id}
        for name in dict.fromkeys(file_names):
            key = name.casefold()
            if key in known or key == row.name.casefold():
                continue
            reason = secret_reason(name)
            clash = self._clash(name, person_id)
            if reason:
                self.note(f"another name for {row.name} was not saved: {reason}.")
                continue
            if clash is not None:
                self.note(f"“{name}” was not saved for {row.name}: it is what Enzo calls #{clash}.")
                continue
            alias = PersonAliasRow(person_id=person_id, alias=name, turn_id=HAND_EDIT)
            self.session.add(alias)
            self.aliases.append(alias)
            known.add(key)
            self.edits.names += 1

    def _clash(self, name: str, person_id: int) -> int | None:
        key = name.casefold()
        for pid, row in self.people.items():
            if pid != person_id and row.name.casefold() == key:
                return pid
        return next(
            (a.person_id for a in self.aliases if a.person_id != person_id and a.alias.casefold() == key),
            None,
        )

    # -- facts

    def _line(self, person_id: int, line: FactLine) -> None:
        if line.id is not None and self._numbered(person_id, line):
            return
        known = next(
            (
                r
                for r in self.facts.values()
                if r.status == "active"
                and r.person_id == person_id
                and r.id not in self.stands
                and self._same(line, r)
            ),
            None,
        )
        if known is None:
            self.changes.append((None, person_id, line))
        else:
            self.stands.add(known.id)  # it lost its number, or is there twice

    def _numbered(self, person_id: int, line: FactLine) -> bool:
        """Match a line by its number; False when that fact already has a line."""
        row = self.facts.get(line.id) if line.id is not None else None
        head = self.current(line.id)
        if row is None or head is None:
            self.note(
                f"a line numbered #{line.id} is for something you asked Enzo to forget, so it "
                "was left out. To keep it, add it again as a new line, without the number."
            )
            return True
        if head.id in self.stands:
            return False
        owner = head.person_id
        if owner != person_id and owner not in self.sections:
            person_id = owner  # its own heading is missing: it is still theirs (see _removed)
        self.stands.add(head.id)
        if owner == person_id and self._same(line, row):
            return True  # unchanged, or an older copy of it: the newer version stands
        if head is not row and self.saved_at <= head.created_at:
            self.note(
                f"your change to #{line.id} was not saved: Enzo learned a newer version in chat "
                f"(#{head.id}), and the newer one counts."
            )
            return True
        self.changes.append((head, person_id, line))
        return True

    def _same(self, line: FactLine, row: PersonFactRow) -> bool:
        if line.kind != row.kind:
            return line.kind == "other" and row.kind == "tie" and False
        if row.kind == "birthday":
            return line.birthday is not None and (
                line.birthday.month,
                line.birthday.day,
                line.birthday.year,
            ) == (row.month, row.day, row.year)
        if row.kind == "tie":
            return line.text == row.text and self._other(line) == row.other_id
        return line.text == row.text

    def _other(self, line: FactLine) -> int | None:
        if line.other is None:
            return None
        key = " ".join(line.other.split()).casefold()
        matches = {pid for pid, row in self.people.items() if row.name.casefold() == key}
        matches |= {a.person_id for a in self.aliases if a.alias.casefold() == key}
        return next(iter(matches)) if len(matches) == 1 else None

    async def _apply_changes(self) -> None:
        for old, person_id, line in self.changes:
            kind, text, other = line.kind, line.text, None
            if kind == "tie":
                other = self._other(line)
                if other is None or other == person_id:
                    kind, text = "other", line.raw  # "Head of: marketing" is a note
            reason = secret_reason(text)
            name = self.people[person_id].name
            if reason:
                what = "changed" if old is not None else "added"
                self.note(
                    f"a line you {what} for {name} was not saved: {reason}, and Enzo never keeps "
                    "those." + (" That fact stays as it was." if old is not None else "")
                )
                if old is not None:
                    self.stands.add(old.id)
                continue
            replaces = old
            if kind in SINGLE or kind == "tie":
                current = self._current_of(person_id, kind, other)
                if current is not None and current is not old:
                    if old is None and current.id in self.stands:
                        label = "relation" if kind == "relation" else kind
                        self.note(
                            f"{name} had two lines for one {label}: Enzo kept the one you added."
                        )
                    if old is not None:
                        old.status = "replaced"
                    replaces = current
            if replaces is not None:
                replaces.status = "replaced"
                await self.session.flush()
            if old is not None:
                self.edits.changed += 1
            else:
                self.edits.added += 1
            born = line.birthday if kind == "birthday" else None
            row = PersonFactRow(
                person_id=person_id,
                kind=kind,
                text=text,
                other_id=other,
                month=born.month if born else None,
                day=born.day if born else None,
                year=born.year if born else None,
                said_by="user",
                replaces=replaces.id if replaces is not None else None,
                turn_id=HAND_EDIT,
            )
            self.session.add(row)
            await self.session.flush()
            self.facts[row.id] = row
            self.stands.add(row.id)
            if replaces is not None:
                self.after[replaces.id] = row
            if born is not None:
                await _move_birthday(self.session, person_id, name, born)

    def _current_of(self, person_id: int, kind: str, other: int | None) -> PersonFactRow | None:
        return next(
            (
                r
                for r in self.facts.values()
                if r.status == "active"
                and r.person_id == person_id
                and r.kind == kind
                and (kind != "tie" or r.other_id == other)
            ),
            None,
        )

    def _no_stamp(self) -> None:
        if not self.missing_once:
            self.missing_once = True
            self.note(
                "People.md had lost the line near its top that tells Enzo which copy it is, so "
                "nothing was removed. To make Enzo forget something, delete just its line."
            )

    async def _removed(self) -> None:
        """Facts and people whose lines the user deleted: only those their copy had."""
        numbers = self.parsed.numbers()
        for person_id, row in list(self.people.items()):
            facts = [
                r for r in self.facts.values() if r.status == "active" and r.person_id == person_id
            ]
            if person_id not in self.sections:
                if any(r.id in numbers for r in facts):
                    self.note(
                        f"the heading for {row.name} (#{person_id}) was missing, so Enzo kept "
                        f"{row.name}. To make Enzo forget someone, delete their heading and every "
                        "line under it."
                    )
                    continue
                if self.written_at is None:
                    self._no_stamp()
                    continue
                if row.created_at <= self.written_at:
                    await _forget_person(self.session, person_id)
                    del self.people[person_id]
                    self.edits.people_removed += 1
                continue
            for fact in facts:
                if fact.id in self.stands:
                    continue
                if self.written_at is None:
                    self._no_stamp()
                    continue
                if fact.created_at <= self.written_at:
                    await _forget_fact(self.session, fact.id)
                    self.edits.removed += 1


def _content_words(text: str) -> list[str]:
    stop = {"the", "and", "for", "who", "what", "does", "about", "with", "from", "that", "this"}
    return [w for w in re.findall(r"[a-z0-9']{3,}", text.casefold()) if w not in stop]


def _words(text: str | None, empty: str) -> str:
    words = " ".join((text or "").split())
    if not words:
        raise PeopleRefusedError(empty)
    reason = secret_reason(words)
    if reason:
        msg = f"Not saved: {reason}, and Enzo never keeps those."
        raise PeopleRefusedError(msg)
    return words


def _keepable(lines: Sequence[str]) -> tuple[list[str], list[str]]:
    """The user's unread lines to keep in the file, and a note for each that cannot be."""
    kept, notes = [], []
    day = f"> {datetime.now(UTC):%-d %b %Y}:"
    for line in lines:
        reason = secret_reason(line)
        if reason:
            notes.append(
                f"{day} a line Enzo could not read as a fact was taken out of the file: "
                f"{reason}, and Enzo never keeps those."
            )
        else:
            kept.append(line)
    return kept, notes


def _stat(path: Path) -> _Seen | None:
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    return _Seen(found.st_mtime_ns, found.st_size, found.st_ino)


def _set_aside(path: Path) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H%M%S")
    target = path.with_name(f"People (unreadable copy {stamp}).md")
    path.replace(target)
    return target.name


def _write_whole(path: Path, body: str) -> _Seen:
    """Write the file next to its place, then swap it in: a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".people-", suffix=".tmp")
    try:
        # mkstemp makes the file readable by the user only: personal details stay theirs.
        import os  # noqa: PLC0415 - only the writer needs it

        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(body)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    seen = _stat(path)
    if seen is None:
        msg = f"{path} went missing as it was written"
        raise FileNotFoundError(msg)
    return seen
