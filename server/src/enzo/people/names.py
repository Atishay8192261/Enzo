"""Who a message is about: the people Enzo knows, by name, by another name, or by how they are
related to the user ("my mom", "my boss"). Words, not meaning: a person named only by meaning is
found by the search index instead (ADR 0036).

Names are matched whole, in any case ("priya" is Priya), except names that are also everyday words
("Will", "May", "Rose"): those only as written, with their capital.
"""

import re
from collections.abc import Iterable, Sequence

from enzo.people.records import Person

#: Names that are everyday words too: matched only as the user wrote the name.
COMMON_WORDS = frozenset(
    {
        "will", "may", "june", "april", "august", "rose", "mark", "bill", "grace", "hope",
        "joy", "faith", "art", "pat", "sunny", "summer", "autumn", "dawn", "eve", "ray",
        "sky", "jack", "frank", "rich", "sue", "ben", "bob", "chase", "drew", "gene", "guy",
        "holly", "iris", "ivy", "jay", "jewel", "lily", "max", "miles", "nick", "olive",
        "pearl", "penny", "rob", "rusty", "ruby", "sandy", "storm", "violet", "wade", "an",
        "al", "ed", "mo", "di", "jo", "kim", "lee", "long", "page", "price", "young", "king",
    }
)  # fmt: skip

#: Ways of saying the same relation: "my mom" is someone whose relation is "mother".
KIN: tuple[frozenset[str], ...] = tuple(
    frozenset(group)
    for group in (
        {"mother", "mom", "mum", "mommy", "mummy", "mama", "mamma", "ma"},
        {"father", "dad", "daddy", "papa", "pa", "pop"},
        {"parent", "parents"},
        {"grandmother", "grandma", "granny", "nana", "nan", "gran"},
        {"grandfather", "grandpa", "granddad", "grandad", "gramps"},
        {"sister", "sis"},
        {"brother", "bro"},
        {"wife"},
        {"husband", "hubby"},
        {"partner", "spouse"},
        {"girlfriend", "gf"},
        {"boyfriend", "bf"},
        {"fiance", "fiancé", "fiancee", "fiancée"},
        {"son"},
        {"daughter"},
        {"kid", "kids", "child", "children"},
        {"aunt", "auntie", "aunty"},
        {"uncle"},
        {"cousin"},
        {"niece"},
        {"nephew"},
        {"friend", "buddy", "pal", "bestie"},
        {"boss", "manager", "supervisor"},
        {"colleague", "coworker", "co-worker", "teammate"},
        {"roommate", "flatmate", "housemate"},
        {"neighbor", "neighbour"},
        {"doctor", "gp", "dentist"},
        {"therapist"},
        {"assistant"},
        {"landlord", "landlady"},
        {"teacher", "tutor"},
        {"client"},
        {"mentor"},
        {"in-law", "inlaw"},
    )
)
_KIN_WORD = {word: group for group in KIN for word in group}
#: "my mom", "my older sister", "my mother-in-law".
_MY = re.compile(
    r"\bmy\s+(?:(?:little|big|older|younger|elder|baby|twin|best|dear|old|new|step|ex)[\s-]+)?"
    r"([a-zé]+(?:-in-law)?)\b",
    re.IGNORECASE,
)
#: Words used as names on their own: "call Mom", "Dad's birthday".
_BARE = re.compile(r"(?<![\w'-])(Mom|Mum|Dad|Mama|Papa|Grandma|Grandpa|Nana|Granny)(?![\w-])")


def kin_groups(relation: str) -> set[frozenset[str]]:
    """The kinds of relation a person's relation names: "older sister" → sister."""
    words = re.findall(r"[a-zé]+(?:-in-law)?|in-law", relation.casefold())
    out = {_KIN_WORD[w] for w in words if w in _KIN_WORD}
    if any(w.endswith("-in-law") for w in words):
        out.add(_KIN_WORD["in-law"])
    return out


def _pattern(name: str) -> re.Pattern[str]:
    words = r"\s+".join(re.escape(w) for w in name.split())
    flags = 0 if name.casefold() in COMMON_WORDS else re.IGNORECASE
    return re.compile(rf"(?<![\w'-]){words}(?![\w-])", flags)


def names_of(person: Person) -> list[str]:
    """Every name someone is known by: full name, first name (3 letters or more), others."""
    out = [person.name]
    first = person.name.split()[0]
    if first != person.name and len(first) >= 3:
        out.append(first)
    out += list(person.aliases)
    return list(dict.fromkeys(n for n in out if n.strip()))


def mentioned(text: str, people: Sequence[Person]) -> list[Person]:
    """The people ``text`` names or speaks of by relation, in the order it first does."""
    found: dict[int, int] = {}
    for person in people:
        for name in names_of(person):
            hit = _pattern(name).search(text)
            if hit is not None:
                found[person.id] = min(found.get(person.id, hit.start()), hit.start())
    said = [(m.start(), m.group(1)) for m in _MY.finditer(text)]
    said += [(m.start(), m.group(1)) for m in _BARE.finditer(text)]
    for at, word in said:
        group = _kin_group(word)
        if group is None:
            continue
        for person in people:
            if person.relation and group in kin_groups(person.relation):
                found[person.id] = min(found.get(person.id, at), at)
    order = sorted(found, key=lambda i: (found[i], i))
    by_id = {p.id: p for p in people}
    return [by_id[i] for i in order]


def _kin_group(word: str) -> frozenset[str] | None:
    word = word.casefold()
    if word.endswith("-in-law"):
        return _KIN_WORD["in-law"]
    return _KIN_WORD.get(word) or _KIN_WORD.get(word.rstrip("s"))


def clashes(name: str, people: Iterable[Person], *, but: int | None = None) -> Person | None:
    """Someone else already known by ``name`` (as a name or another name), if anyone."""
    key = name.casefold()
    for person in people:
        if person.id != but and any(n.casefold() == key for n in person.names):
            return person
    return None
