"""Watches end to end (spec §9.4 Verify): set up with one yes, a dry run shown, turned on with a
second yes; silent while the page stays the same; one wake when it changes, reported where it was
set; no second wake after a restart; a 429 stops it with a report."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from enzo.agent.service import ConversationService
from enzo.brain import Message, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.brain.router import Router
from enzo.hooks.sources import PageTarget
from enzo.hooks.store import Hooks
from enzo.hooks.tools import watch_tools
from enzo.hooks.watcher import Watcher
from enzo.schedule import Background, Bus, Runner, Schedule
from enzo.store.repository import Store
from enzo.tools.registry import ToolRegistry
from tests.gateway.test_api import client, config, events, new_conversation
from tests.hooks.test_sources import SHOP, URL, Site


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def plan(**more: Any) -> ToolCall:
    args = {
        "what": "the blue kettle's price",
        "source": "page",
        "url": URL,
        "selector": ".price",
        "rule": "below",
        "threshold": 40,
        "every_minutes": 10,
        "times": 1,
        "then": "Tell me the new price.",
        **more,
    }
    return ToolCall(id="w1", name="watch", arguments_json=json.dumps(args))


class World:
    def __init__(self, store: Store, brain: ScriptedBrain, site: Site) -> None:
        self.clock = Clock()
        self.shown: list[str] = []
        self.store, self.site = store, site
        self.bus = Bus()
        self.schedule = Schedule(store.engine)
        self.runner = Runner(self.schedule, self.bus, clock=self.clock)
        self.watcher = self._watcher()
        self.registry = ToolRegistry(watch_tools(self.watcher))
        router = Router(config(), build=lambda _c: brain, available=lambda _c: True)
        self.service = ConversationService(store, router, self.registry, system_prompt="Be brief.")
        self.watcher.connect(self.service, self.runner.poke)

    def _watcher(self) -> Watcher:
        async def notify(_title: str, body: str) -> bool:
            self.shown.append(body)
            return True

        sources = Site.sources(self.site)
        watcher = Watcher(
            Hooks(self.store.engine), sources, self.bus, notify=notify, clock=self.clock
        )
        self.runner.add(watcher)
        return watcher

    def restart(self) -> None:
        """A new gateway on the same database: nothing in memory survives."""
        self.runner = Runner(self.schedule, self.bus, clock=self.clock)
        self.watcher = self._watcher()
        self.watcher.connect(self.service, self.runner.poke)

    async def ticks(self, n: int, every: timedelta = timedelta(minutes=10)) -> None:
        for _ in range(n):
            self.clock.now += every
            await self.runner.tick()
            await self.watcher.settle()
            await self.runner.tick()  # a change seen in this tick is acted on now
            await self.watcher.settle()


@pytest.fixture
def site() -> Site:
    return Site({URL: (200, SHOP)})


async def test_set_up_turned_on_silent_until_it_changes_then_one_wake(
    store: Store, site: Site
) -> None:
    brain = ScriptedBrain(
        [
            Message.assistant(tool_calls=(plan(),)),
            Message.assistant(
                tool_calls=(ToolCall(id="e1", name="enable_watch", arguments_json='{"id": 1}'),)
            ),
            Message.assistant("It is on. I'll tell you when it drops below $40."),
            Message.assistant("The blue kettle is now $39.00, under your $40."),  # the wake
        ]
    )
    world = World(store, brain, site)
    bg = Background(world.schedule, world.bus, world.runner, world.watcher)
    async with client(world.service, background=bg) as c:
        cid = await new_conversation(c)
        got = events(
            (
                await c.post(
                    f"/conversations/{cid}/turns",
                    json={"message": "tell me when the kettle is under $40", "timezone": "UTC"},
                )
            ).text
        )
        (card,) = [d for k, d in got if k == "approval_needed"]
        assert card["summary"].startswith("Set up a watch (it starts off; turning it on asks")
        assert "every 10 minutes, at most 1 wake, without using a model" in card["summary"]
        assert card["detail"] == "Right now it reads: $59.99"  # read once before asking
        assert (await c.get("/watches")).json()["watches"] == []  # nothing before the yes
        await c.post(f"/conversations/{cid}/approvals/{card['id']}", json={"approved": True})
        got = events((await c.post(f"/conversations/{cid}/resume")).text)
        (watch,) = (await c.get("/watches")).json()["watches"]
        assert (watch["id"], watch["status"]) == (1, "off")  # saved off (spec §9.4)
        (card,) = [d for k, d in got if k == "approval_needed"]  # the second yes
        assert card["summary"].startswith("Turn on watch #1: the blue kettle's price.")
        assert card["detail"].startswith("Right now it reads: $59.99\nChecked ")
        assert "It wakes Enzo when the number in it goes below 40" in card["detail"]
        world.clock.now += timedelta(minutes=2)  # the user reads the card, then says yes
        await c.post(f"/conversations/{cid}/approvals/{card['id']}", json={"approved": True})
        got = events((await c.post(f"/conversations/{cid}/resume")).text)
        assert [k for k, _ in got if k == "approval_needed"] == []  # the same reading: no re-ask
        (watch,) = (await c.get("/watches")).json()["watches"]
        assert watch["status"] == "on"
        brain_calls, fetched = len(brain.calls), site.asked.count(URL)

        await world.ticks(3)  # unchanged: silent, and no model call
        assert len(brain.calls) == brain_calls
        assert world.shown == []
        assert (await c.get(f"/conversations/{cid}/scheduled")).json() == []
        assert site.asked.count(URL) == fetched + 3  # one check per tick, nothing more

        site.pages[URL] = (200, SHOP.replace("$59.99", "$39.00"))
        await world.ticks(1)
        assert world.shown == [
            "the blue kettle's price: The blue kettle is now $39.00, under your $40. "
            "That was its last wake."
        ]
        (notice,) = (await c.get(f"/conversations/{cid}/scheduled")).json()
        assert (notice["kind"], notice["outcome"]) == ("watch", "reported")
        own = notice["chat_id"]
        stored = (await c.get(f"/conversations/{own}/messages")).json()
        assert stored[0]["origin"] == "watch"  # the apps show a watch, not the user typing
        assert stored[0]["parts"][0]["text"] == "Tell me the new price."
        note = stored[0]["parts"][1]["text"]
        assert "Watch #1 saw a change" in note
        assert '<untrusted-data source="watch"' in note  # the readings are fenced
        assert "$59.99" in note
        assert "$39.00" in note

        world.restart()  # a restart never acts on the same change twice
        await world.ticks(3)
        assert len(world.shown) == 1
        assert (await c.get("/watches")).json()["watches"] == []  # its last wake ended it


async def test_a_rate_limit_stops_it_and_says_so(store: Store, site: Site) -> None:
    world = World(store, ScriptedBrain([]), site)
    hooks = world.watcher.hooks
    made = await hooks.create(
        what="the kettle",
        target=PageTarget(url=URL, selector=".price"),
        rule="changes",
        instruction="Tell me",
        interval_s=600,
        timezone="UTC",
        max_wakes=5,
    )
    dry = await world.watcher.check(made.id)
    await hooks.enable(made.id, dry.token, world.clock.now)
    seen: list[dict[str, Any]] = []
    world.bus.publish = lambda event: seen.append(event.data)  # type: ignore[method-assign]
    site.pages[URL] = (429, "slow down")
    await world.ticks(1)
    (stop,) = seen
    assert stop["outcome"] == "stopped"
    assert "said too many requests" not in stop["words"]  # the site's own words, plainly
    assert stop["words"].startswith("Enzo stopped watching the kettle: shop.example answered")
    assert world.shown == [stop["words"]]
    assert (await hooks.get(made.id)).status == "stopped"  # type: ignore[union-attr]
    await world.ticks(3)
    assert site.asked.count(URL) == 2  # the dry run and the one check: nothing retried
