"""Goals, reminders and tasks about someone (ADR 0036): written in the same transaction as the
goal or job they are about, so a link never points at nothing."""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enzo.store.schema import PersonLinkRow, PersonRow


class UnknownPersonError(Exception):
    """A plain-language reason: a number that is not someone Enzo knows."""


async def link_people(
    session: AsyncSession,
    people: Sequence[int],
    *,
    goal_id: int | None = None,
    job_id: int | None = None,
    days_before: int | None = None,
) -> None:
    """Link a goal or a job to these people; refused when one of them is unknown."""
    wanted = list(dict.fromkeys(people))
    if not wanted:
        return
    known = await session.scalar(
        select(func.count()).select_from(PersonRow).where(PersonRow.id.in_(wanted))
    )
    if known != len(wanted):
        msg = "One of those people is not someone Enzo knows: find them first, by their number."
        raise UnknownPersonError(msg)
    for person in wanted:
        session.add(
            PersonLinkRow(person_id=person, goal_id=goal_id, job_id=job_id, days_before=days_before)
        )
