"""The conversation service on its own: one turn at a time per chat, even when two messages
arrive at the same moment."""

import asyncio

from enzo.agent.service import TurnInProgressError
from enzo.brain import Message
from enzo.brain.fake import ScriptedBrain
from enzo.store.repository import Store
from tests.gateway.test_api import service


async def test_two_messages_sent_at_once_never_start_two_turns(store: Store) -> None:
    """Regression: the chat was only marked busy once its turn had started, after several awaits
    (history, memory), so two sends at the same moment both started a turn and interleaved the
    transcript. A helper's report arriving while the user types does exactly that."""
    brain = ScriptedBrain([Message.assistant("one"), Message.assistant("two")])
    svc = service(store, {"m-main": brain})
    cid = await store.create_conversation()
    got = await asyncio.gather(svc.send(cid, "a"), svc.send(cid, "b"), return_exceptions=True)
    refused = [g for g in got if isinstance(g, TurnInProgressError)]
    started = [g for g in got if not isinstance(g, BaseException)]
    assert (len(refused), len(started)) == (1, 1)
    assert [type(e).__name__ async for e in started[0]][-1] == "TurnDone"
    await svc.wait_idle(cid)
    assert not svc.running(cid)
    # A failed start frees the chat: the next message goes through.
    assert [type(e).__name__ async for e in await svc.send(cid, "c")][-1] == "TurnDone"
