"""Private details in text that is about to leave the computer (spec §12.5, ADR 0029).

Deterministic on purpose: patterns for kinds of private detail (health, money, relationships,
religion and politics, addresses, ID numbers, secrets, contact details), plus what Enzo knows to
be private from the user's own records: the names of people in their life (memory and this
chat), their address, the memories themselves, and the rules they set. No model call: it costs
nothing, sends the text nowhere, and gives the same answer every time.

What counts depends on how the text leaves (``Channel``):

- **message** (an email, an invite, a comment, a post): every private kind, and memories quoted
  word for word. Contact details are left alone: a receiver needs them to answer.
- **field** (typing into a web form): the same, minus what the field itself asks for (an address
  typed into "Delivery address" is what the site needs; one typed into "Special requests" is not).
- **search** and **address** (a search query, a page's address): identifiers always (names,
  addresses, ID numbers, contact details, secrets); a topic (health, money, …) only when the text
  ties it to the user ("my wife is pregnant"), since "pregnancy-safe restaurants" says nothing
  about anyone.

A rule the user set covers its kinds and words everywhere, and nothing a field asks for excuses it.
Limits (ADR 0029): words are matched, not meaning. A detail said in other words ("we're expecting")
is caught only if a pattern knows the phrase; a word used in another sense ("a cancer survivor
charity run") is flagged anyway. A flag is a note or a question, never a block.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from enzo.memory.guard import KEY_SHAPES
from enzo.tools.registry import Channel, Outgoing

type Kind = Literal[
    "health",
    "money",
    "people",
    "relationships",
    "beliefs",
    "location",
    "ids",
    "contact",
    "secret",
    "memory",
    "rule",
]
#: The kinds a rule can name (the user's words "my health", "money", "my family").
type RuleKind = Literal["health", "money", "people", "relationships", "beliefs", "location", "ids"]
RULE_KINDS: tuple[RuleKind, ...] = (
    "health",
    "money",
    "people",
    "relationships",
    "beliefs",
    "location",
    "ids",
)

#: How a card names each kind: "This includes <label>: “…”".
LABELS: dict[Kind, str] = {
    "health": "a health detail",
    "money": "a money detail",
    "people": "the name of someone in your life",
    "relationships": "a relationship detail",
    "beliefs": "your religion or politics",
    "location": "where you live",
    "ids": "an ID or account number",
    "contact": "a phone number or email address",
    "secret": "a password, key or code",
    "memory": "something Enzo remembers about you",
    "rule": "something you asked Enzo to keep private",
}

_I = re.IGNORECASE

_KIN = (
    r"wife|husband|partner|spouse|son|daughter|kids?|child|children|baby|mom|mum|mother|dad|"
    r"father|parents?|brother|sister|siblings?|grandma|grandmother|grandpa|grandfather|aunt|"
    r"uncle|cousin|niece|nephew|fianc[eé]e?|girlfriend|boyfriend|friend|boss|colleague|"
    r"coworker|co-worker|roommate|flatmate|neighbou?r|doctor|therapist"
)
KIN = re.compile(rf"\b({_KIN})\b", _I)
_NAME = r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
#: "my sister Priya", "my wife, Sara": a name the user gave someone in their life.
_KIN_NAME = re.compile(rf"\b(?i:my\s+({_KIN}))\s*,?\s+{_NAME}")
#: The same in a memory's words, which leave out "my": "Wife Sara is pregnant".
_KIN_NAME_LOOSE = re.compile(rf"\b(?i:({_KIN}))\s*,?\s+(?:(?i:is|named|called)\s+)?{_NAME}")
#: The text speaks of the user or their people: a topic in it is about them.
_PERSONAL = re.compile(
    rf"\b(i|i'?m|im|i'?ve|i'?d|i'?ll|me|my|mine|myself|we|we'?re|our|ours|us|{_KIN})\b", _I
)

_TOPICS: dict[Kind, re.Pattern[str]] = {
    "health": re.compile(
        r"\b(pregnan(?:t|cy)|expecting a baby|miscarri(?:age|ed)|ivf|fertility|"
        r"trying to conceive|diagnos(?:ed|is)|cancer|chemo(?:therapy)?|tumou?r|diabet(?:es|ic)|"
        r"hiv|hepatitis|herpes|depress(?:ed|ion)|anxiety|panic attacks?|bipolar|schizophreni\w*|"
        r"adhd|autis(?:m|tic)|ptsd|ocd|eating disorder|anorexi\w*|bulimi\w*|insomnia|"
        r"epilep\w*|seizures?|asthma|allerg(?:y|ies|ic)|c[o]?eliac|gluten intoleran\w*|"
        r"lactose intoleran\w*|migraines?|dementia|alzheimer'?s|parkinson'?s|heart attack|"
        r"had a stroke|surgery|in (?:the )?hospital|hospitali[sz]ed|rehab|addict(?:ion|ed)|"
        r"alcoholi(?:c|sm)|sober|sobriety|therapy|therapist|psychiatri\w*|antidepressants?|"
        r"medication|prescription|insulin|disabilit(?:y|ies)|disabled|wheelchair|"
        r"menopause|erectile|sexual health|mental health|chronic (?:pain|illness))\b",
        _I,
    ),
    "money": re.compile(
        r"\b(salary|my income|our income|i earn|we earn|i make \$?\d|in debt|debts?|loans?|"
        r"mortgage|bankrupt(?:cy)?|overdra(?:ft|wn)|credit score|net worth|my savings|"
        r"our savings|can'?t afford|cannot afford|unemployed|laid off|lost (?:my|his|her|our) "
        r"job|alimony|child support|inheritance|tax return|bank balance|account balance|"
        r"paycheck|paycheque|payday loan|behind on (?:rent|payments|bills))\b",
        _I,
    ),
    "relationships": re.compile(
        r"\b(divorc(?:e|ed|ing)|separated|my ex|ex-(?:wife|husband|partner|girlfriend|"
        r"boyfriend)|affair|cheat(?:ed|ing) on|broke up|break-?up|custody|restraining order|"
        r"gay|lesbian|bisexual|queer|transgender|non-binary|coming out|came out|widow(?:ed|er)?)"
        r"\b",
        _I,
    ),
    "beliefs": re.compile(
        r"\b(?:(?:i'?m|i am|we'?re|we are)\s+(?:a\s+)?(?:muslim|christian|catholic|jewish|hindu|"
        r"buddhist|sikh|mormon|atheist|agnostic|democrat|republican|conservative|liberal|"
        r"socialist)|kosher|halal|(?:my|our)\s+(?:religion|faith|church|mosque|synagogue|"
        r"temple)|vot(?:e|ed|ing)\s+(?:for\s+)?(?:the\s+)?(?:democrats?|republicans?|labour|"
        r"conservatives?|tor(?:y|ies)|greens?|reform))\b",
        _I,
    ),
}

#: Where the user lives, as the text says it.
_HOME = re.compile(
    r"\b(?:(?:i|we)\s+live\s+(?:at|in|on|near)\s+[^.,;\n]{2,60}|"
    r"(?:my|our)\s+(?:home\s+)?address\s+(?:is\s+)?[^.;\n]{2,60}|"
    r"(?:my|our)\s+(?:house|home|flat|apartment)\s+(?:is\s+)?(?:at|on)\s+[^.,;\n]{2,60})",
    _I,
)
_STREET = re.compile(
    r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Z][\w'-]*\s+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|"
    r"Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl|Terrace|Close|Crescent|Square|Sq|"
    r"Parkway|Pkwy|Highway|Hwy|Gardens|Grove|Row|Mews)\b\.?"
)
_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b")

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_DIGIT_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")
_ID_PHRASE = re.compile(
    r"\b(?:passport|driver'?s licen[cs]e|licen[cs]e|national insurance|social security|"
    r"tax id|nhs|medicare|medical record|account|routing|sort code|member(?:ship)?)"
    # The number itself: capitals and digits, in groups ("X1234567", "QQ 12 34 56 C").
    r"\s*(?:number|no\.?|#)\s*(?:is\s*|:\s*)?(?-i:[A-Z0-9][A-Z0-9-]{1,}(?: [A-Z0-9][A-Z0-9-]*)*)",
    _I,
)
#: An ID number has digits; "account number for the booking" names none.
MIN_ID_DIGITS = 3
_BIRTH = re.compile(r"\b(?:date of birth|d\.?o\.?b\.?|born on)\b[:\s]*[\w ,/.-]{4,20}", _I)
_PASSWORD = re.compile(
    r"\b(?:password|passcode|passphrase|pin|one[- ]time code|verification code|security code|"
    r"2fa code|cvv|cvc|api key|secret key|access token|recovery code)s?\s*(?:is|was|:|=)\s*\S+",
    _I,
)
_PHONE = re.compile(r"(?<![\w+])\+?\d[\d ().-]{6,}\d(?!\w)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")

#: What a form field asks for, by its label, name or autocomplete hint: a detail of that kind
#: typed into it is what the site needs.
_FIELD_ASKS: dict[Kind, re.Pattern[str]] = {
    "location": re.compile(
        r"address|street|line ?[12]|city|town|zip|postal|post ?code|county|state|province|"
        r"region|country|location|deliver|shipping|billing",
        _I,
    ),
    "contact": re.compile(r"phone|mobile|tel\b|email|e-mail|contact", _I),
    "people": re.compile(
        r"name|guest|recipient|attendee|passenger|travell?er|contact|nickname", _I
    ),
    "health": re.compile(
        r"allerg|dietary|diet\b|medical|health|condition|accessib|disab|special assistance", _I
    ),
    "ids": re.compile(
        r"passport|licen[cs]e|ssn|social security|national|tax|card|account|iban|routing|"
        r"sort code|birth|dob|bday|id number|identity|member",
        _I,
    ),
    "money": re.compile(r"income|salary|earn|amount|loan|debt|savings", _I),
    "secret": re.compile(
        r"pass(?:word|code|phrase)|\bpin\b|otp|one.?time|verification|\bcode\b|cvc|cvv|"
        r"security|current-password|new-password",
        _I,
    ),
}

_TOPIC_KINDS: frozenset[Kind] = frozenset(_TOPICS)
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "am",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "it",
        "they",
        "them",
        "his",
        "her",
        "their",
        "this",
        "that",
        "these",
        "those",
        "as",
        "so",
        "if",
        "then",
        "than",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "not",
        "no",
        "yes",
        "very",
        "just",
        "also",
    }
)
#: Words in a row a message must share with a memory to count as quoting it.
MEMORY_RUN = 4


@dataclass(frozen=True, slots=True)
class Person:
    """Someone in the user's life Enzo knows by name."""

    name: str
    #: How they relate to the user, when Enzo knows: "wife", "sister".
    relation: str | None = None
    #: The memory Enzo knows them from; None: the user named them in this chat.
    memory: int | None = None


@dataclass(frozen=True, slots=True)
class Remembered:
    """A memory, for spotting a message that quotes it."""

    id: int
    text: str


@dataclass(frozen=True, slots=True)
class RuleTerms:
    """What one of the user's rules covers."""

    id: int
    text: str
    kinds: frozenset[RuleKind] = frozenset()
    words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Known:
    """What Enzo knows is private: from the user's memory, this chat, and their rules."""

    people: tuple[Person, ...] = ()
    #: Street addresses and postcodes the user gave (memory or this chat): theirs.
    addresses: tuple[str, ...] = ()
    memories: tuple[Remembered, ...] = ()
    rules: tuple[RuleTerms, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    kind: Kind
    #: The words in the outgoing text, made short and printable; None: never repeated (a secret).
    words: str | None
    #: True: an ungated send must ask first. False: a note on a card only.
    asks: bool = True
    #: "your wife"; or which memory it quotes.
    relation: str | None = None
    memory: int | None = None
    #: The user's rule that covers it.
    rule: RuleTerms | None = None
    #: Where it is in the text (to tell overlapping findings apart).
    span: tuple[int, int] = field(default=(0, 0), compare=False)


def detect(outgoing: Outgoing, known: Known) -> list[Finding]:
    """The private details in one outgoing text, as its channel counts them."""
    text = outgoing.text
    if not text.strip():
        return []
    found = [
        *_topics(text),
        *_identifiers(text, known),
        *_people(text, known),
        *_rule_words(text, known),
    ]
    if outgoing.channel == "message":
        found += _quoted_memories(text, known, found)
    return _for_channel(found, outgoing, known)


def detect_all(outgoing: Iterable[Outgoing], known: Known) -> list[Finding]:
    """Every outgoing text's findings, each private detail once."""
    out: list[Finding] = []
    seen: set[tuple[str, str | None, int | None]] = set()
    for item in outgoing:
        for f in detect(item, known):
            key = (f.kind, (f.words or "").casefold() or None, f.memory)
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def people_in(texts: Iterable[str], *, loose: bool = False) -> list[Person]:
    """People the user named: "my sister Priya" in a message; with ``loose``, also "Sister
    Priya" (a memory's words)."""
    pattern = _KIN_NAME_LOOSE if loose else _KIN_NAME
    found: dict[str, Person] = {}
    for text in texts:
        for m in pattern.finditer(text):
            name = m.group(2)
            if name.casefold() not in _STOP:
                found.setdefault(name, Person(name, m.group(1).lower()))
    return list(found.values())


def addresses_in(texts: Iterable[str]) -> list[str]:
    """Street addresses and postcodes in the user's own words (memory, this chat)."""
    out: list[str] = []
    for text in texts:
        out += [" ".join(m.group().split()).rstrip(".") for m in _STREET.finditer(text)]
        out += [m.group() for m in _POSTCODE.finditer(text)]
    return list(dict.fromkeys(a for a in out if len(a) >= 5))


def is_name(about: str) -> bool:
    """A memory's "about" that names a person ("Priya", "Sam Lee"), not "my wife" or "work"."""
    words = about.split()
    return (
        0 < len(words) <= 4
        and all(w[:1].isupper() for w in words)
        and not KIN.fullmatch(about)
        and about.casefold() not in _STOP
    )


def relation_of(text: str) -> str | None:
    """How a person relates to the user, from a memory's words ("Sister, lives in Austin")."""
    found = KIN.search(text)
    return found.group(1).lower() if found else None


# ---------- the detectors ----------


def _topics(text: str) -> list[Finding]:
    return [
        Finding(kind, _clause(text, m.start(), m.end()), span=(m.start(), m.end()))
        for kind, pattern in _TOPICS.items()
        for m in pattern.finditer(text)
    ]


def _identifiers(text: str, known: Known) -> list[Finding]:
    out = _addresses(text, known) + _numbers(text)
    for m in _EMAIL.finditer(text):
        out.append(Finding("contact", _short(m.group()), span=(m.start(), m.end())))
    for m in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 8 <= len(digits) <= 15 and not _within(m.start(), out):
            out.append(Finding("contact", _short(m.group()), span=(m.start(), m.end())))
    return out


def _addresses(text: str, known: Known) -> list[Finding]:
    """Where the user lives: as the text says it, or an address of theirs Enzo knows."""
    out = [Finding("location", _short(m.group()), span=m.span()) for m in _HOME.finditer(text)]
    for address in known.addresses:
        for m in re.finditer(rf"\b{_loose(address)}\b", text, _I):
            out.append(Finding("location", _short(m.group()), span=(m.start(), m.end())))
    return out


def _numbers(text: str) -> list[Finding]:
    """ID, account and card numbers (masked), and secrets (never repeated)."""
    out: list[Finding] = []
    for pattern in (_SSN, _IBAN, _ID_PHRASE, _BIRTH):
        for m in pattern.finditer(text):
            if pattern is _ID_PHRASE and len(re.findall(r"\d", m.group())) < MIN_ID_DIGITS:
                continue
            out.append(Finding("ids", _masked(m.group()), span=(m.start(), m.end())))
    for m in _DIGIT_RUN.finditer(text):
        if _luhn(re.sub(r"\D", "", m.group())):
            out.append(Finding("ids", _masked(m.group()), span=(m.start(), m.end())))
    for pattern in (KEY_SHAPES, _PASSWORD):
        for m in pattern.finditer(text):
            out.append(Finding("secret", None, span=(m.start(), m.end())))
    return out


def _people(text: str, known: Known) -> list[Finding]:
    out: list[Finding] = []
    named = {p.name: p for p in known.people}
    for m in _KIN_NAME.finditer(text):  # "my sister Priya", even if Enzo never heard of her
        named.setdefault(m.group(2), Person(m.group(2), m.group(1).lower()))
    for person in named.values():
        names = {person.name}
        first = person.name.split()[0]
        if len(first) >= 3:  # "Priya" stands for "Priya Sharma"
            names.add(first)
        for name in sorted(names, key=len, reverse=True):
            # Case matters: a name is written with a capital ("Will", not "will").
            hit = re.search(rf"(?<![\w'-]){re.escape(name)}(?![\w'-])", text)
            if hit is not None:
                relation = f"your {person.relation}" if person.relation else None
                out.append(
                    Finding(
                        "people",
                        _short(hit.group()),
                        relation=relation,
                        memory=person.memory,
                        span=(hit.start(), hit.end()),
                    )
                )
                break
    return out


def _rule_words(text: str, known: Known) -> list[Finding]:
    out: list[Finding] = []
    for rule in known.rules:
        for word in rule.words:
            m = re.search(rf"(?<!\w){_loose(word)}(?!\w)", text, _I)
            if m is not None:
                out.append(
                    Finding("rule", _clause(text, m.start(), m.end()), rule=rule, span=m.span())
                )
    return out


def _quoted_memories(text: str, known: Known, found: Sequence[Finding]) -> list[Finding]:
    """A message that repeats a memory word for word: private context passed on (§12.5)."""
    words = [(m.group(), m.start(), m.end()) for m in _WORD.finditer(text.lower())]
    out: list[Finding] = []
    for memory in known.memories:
        runs = _runs(_WORD.findall(memory.text.lower()))
        for i in range(len(words) - MEMORY_RUN + 1):
            run = tuple(w for w, _s, _e in words[i : i + MEMORY_RUN])
            if run in runs:
                start, end = words[i][1], words[i + MEMORY_RUN - 1][2]
                if not _within(start, found):
                    out.append(
                        Finding(
                            "memory",
                            _clause(text, start, end),
                            asks=False,
                            memory=memory.id,
                            span=(start, end),
                        )
                    )
                break
    return out


def _runs(words: Sequence[str]) -> set[tuple[str, ...]]:
    """Runs of words that say something (not only "and the of"), to match a quote by."""
    out: set[tuple[str, ...]] = set()
    for i in range(len(words) - MEMORY_RUN + 1):
        run = tuple(words[i : i + MEMORY_RUN])
        if sum(w not in _STOP for w in run) >= 2:  # two words that say something
            out.add(run)
    return out


# ---------- what counts, by how the text leaves ----------


def _for_channel(found: Sequence[Finding], outgoing: Outgoing, known: Known) -> list[Finding]:
    channel = outgoing.channel
    personal = bool(_PERSONAL.search(outgoing.text)) or any(f.kind == "people" for f in found)
    out: list[Finding] = []
    for f in found:
        rule = f.rule or _covering(f.kind, known)
        if rule is not None:  # the user's own rule: always said, whatever the field asks
            out.append(_with(f, rule=rule, asks=True))
            continue
        if not _counts(f.kind, channel, personal=personal):
            continue
        if channel == "field" and _field_asks(f.kind, outgoing.field):
            continue
        out.append(f)
    return out


def _counts(kind: Kind, channel: Channel, *, personal: bool) -> bool:
    if channel in ("search", "address"):
        if kind in _TOPIC_KINDS:
            return personal  # "pregnancy-safe restaurants" is no one's secret
        return kind != "memory"
    return kind != "contact"  # a receiver needs a way to answer


def _covering(kind: Kind, known: Known) -> RuleTerms | None:
    return next((r for r in known.rules if kind in r.kinds), None)


def _field_asks(kind: Kind, field_words: str | None) -> bool:
    pattern = _FIELD_ASKS.get(kind)
    return bool(field_words and pattern and pattern.search(field_words))


def _with(f: Finding, *, rule: RuleTerms, asks: bool) -> Finding:
    return Finding(f.kind, f.words, asks, f.relation, f.memory, rule, f.span)


# ---------- words ----------

_BOUNDARY = re.compile(r"[.!?;:,\n]|\s[\u2014\u2013-]\s")
MAX_WORDS = 80


def _clause(text: str, start: int, end: int) -> str:
    """The clause around a match ("my wife is pregnant"), short enough for a card line."""
    left = max((m.end() for m in _BOUNDARY.finditer(text, 0, start)), default=0)
    right = next((m.start() for m in _BOUNDARY.finditer(text, end)), len(text))
    clause = text[left:right]
    if len(clause) > MAX_WORDS:
        middle = (start - left + end - left) // 2
        a = max(0, middle - MAX_WORDS // 2)
        clause = text[left + a : left + a + MAX_WORDS]
    return _short(clause)


def _short(text: str, limit: int = MAX_WORDS) -> str:
    line = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return line if len(line) <= limit else f"{line[: limit - 1].rstrip()}…"


def _masked(value: str) -> str:
    """An ID as a card says it: never in full."""
    tail = re.sub(r"[^A-Za-z0-9]", "", value)[-4:]
    return f"ending {tail}" if len(tail) == 4 else "hidden"


def _loose(words: str) -> str:
    """A phrase as a pattern that allows any spacing between its words."""
    return r"\s+".join(re.escape(w) for w in words.split())


def _within(at: int, found: Iterable[Finding]) -> bool:
    return any(f.span[0] <= at < f.span[1] for f in found)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0
