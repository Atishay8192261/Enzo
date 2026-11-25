"""Live events for open apps (``GET /events``): a reminder appears in the chat on screen."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

#: A slow app never holds the gateway up: past this many unread events it misses the oldest.
QUEUE = 100


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    data: dict[str, Any]


class Bus:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Event]] = set()

    def publish(self, event: Event) -> None:
        for queue in self._queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(QUEUE)
        self._queues.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._queues)
