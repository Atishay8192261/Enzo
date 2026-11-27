"""A deterministic embedder for tests: meaning from a small table of concepts, no downloads.

Each concept is one dimension; a word counts toward every concept it belongs to, so
"shrimp" and "shellfish" share a dimension while having no word in common. Words outside the
table land on hashed dimensions of their own, so unrelated texts stay far apart. It stands in for
a real model's behaviour on the cases the tests need; the real model is tested on the same cases
in ``test_local_model.py``.
"""

import hashlib
import math
import re
from collections.abc import Sequence

from enzo.semantic import EmbedderNotReadyError, Tuning, Use
from enzo.semantic.embedder import State

CONCEPTS: dict[str, set[str]] = {
    "seafood": {"shellfish", "shrimp", "prawn", "prawns", "scampi", "lobster", "crab"},
    "allergy": {"allergic", "allergy", "allergies", "eat", "food"},
    "flying": {"flight", "flights", "plane", "airline", "fly", "flying", "airport"},
    "theatre": {"theatre", "theater", "play", "show", "stage", "musical"},
    "seat": {"seat", "seats", "sit", "sitting", "aisle", "window", "row"},
    "noodles": {"ramen", "noodle", "noodles", "soup", "pho", "udon"},
    "dining": {"restaurant", "restaurants", "place", "spot", "eatery", "dinner"},
    "travel": {"lisbon", "portugal", "trip", "travel", "pack", "packing", "holiday"},
    "clothes": {"clothes", "jacket", "layers", "shoes", "wear", "bring"},
    "boss": {"manager", "boss", "lead", "supervisor"},
    "sister": {"sister", "sibling", "ana"},
    "car": {"car", "drive", "drives", "civic", "vehicle"},
}
#: Hashed dimensions for words outside the table, after the concepts.
SPARE = 64
DIMS = len(CONCEPTS) + SPARE
_WORD = re.compile(r"[a-z]+")
#: Common words carry no meaning here (as real models mostly ignore them).
_COMMON = {
    "a", "an", "the", "i", "me", "my", "you", "your", "we", "our", "is", "are", "was", "to",
    "of", "in", "on", "at", "for", "and", "or", "can", "what", "where", "which", "who", "do",
    "did", "does", "that", "this", "it", "be", "with", "any", "like", "likes", "prefers",
    "user", "enzo", "how", "should", "tonight", "again", "said", "say",
}  # fmt: skip


def vector(text: str) -> list[float]:
    raw = [0.0] * DIMS
    for word in _WORD.findall(text.lower()):
        if word in _COMMON:
            continue
        hit = False
        for i, members in enumerate(CONCEPTS.values()):
            if word in members:
                raw[i] += 1.0
                hit = True
        if not hit:
            spare = int(hashlib.sha256(word.encode()).hexdigest(), 16) % SPARE
            raw[len(CONCEPTS) + spare] += 0.35
    norm = math.sqrt(sum(x * x for x in raw))
    if norm == 0:
        raw[-1], norm = 1.0, 1.0
    return [x / norm for x in raw]


#: Floors for this embedder's spread (concept overlap gives clear gaps).
TUNING = Tuning(memory_floor=0.30, chat_floor=0.35, search_floor=0.20, margin=0.15)


class ConceptEmbedder:
    def __init__(self, model: str = "concepts@1", *, ready: bool = True) -> None:
        self._id = model
        self._ready = ready
        self.prepared = 0
        #: Every batch it was asked to embed, to check what was (and was not) embedded.
        self.batches: list[tuple[Use, list[str]]] = []
        self.problem: str | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def tuning(self) -> Tuning:
        return TUNING

    @property
    def state(self) -> State:
        return "ready" if self._ready else "preparing"

    @property
    def ready(self) -> bool:
        return self._ready

    async def prepare(self) -> bool:
        self.prepared += 1
        self._ready = True
        return True

    async def embed(self, texts: Sequence[str], *, use: Use) -> list[list[float]]:
        if not self._ready:
            raise EmbedderNotReadyError(self._id)
        self.batches.append((use, list(texts)))
        return [vector(t) for t in texts]

    def documents(self) -> list[str]:
        return [t for use, batch in self.batches if use == "document" for t in batch]
