"""A workflow is a plan written in code (spec §9.6, ADR 0032): named steps, each a helper's turn
with a fixed brief, a fixed set of tools and its own approvals, in a fixed order.

- A step's brief is a template: ``{input.pages}`` (what the user gave), ``{item}`` (one item of a
  fan-out step, which runs once per item of a list input) and ``{steps.read}`` (an earlier step's
  output, fenced as data). Nothing else is filled in, and filled-in text is never read again as a
  template, so a page or an input cannot add a placeholder.
- The definition is checked when it is made: names, placeholders, the order of steps, tools.
- Its ``fingerprint`` is a hash of the whole definition, stored with every run. Changing a
  definition means bumping its ``version`` (a test pins each built-in's fingerprint).
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from enzo.tools.fence import fence

NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
KEY = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
SLOT = re.compile(r"\{([^{}]*)\}")
#: A step never hands work on or starts a workflow: nesting stops at the step (spec §9.5).
FORBIDDEN = frozenset({"delegate", "stop_helper", "run_workflow", "stop_workflow"})
MAX_ITEM_CHARS = 500
MAX_TEXT_CHARS = 2000
#: How a list input's items are told apart when it arrives as one text.
SPLIT = re.compile(r"[\s,]+")
#: Values of the inputs a run was given: a text, or a list for a list input.
type Inputs = dict[str, str | list[str]]


class WorkflowInputError(ValueError):
    """What is wrong with the inputs, in plain words (for the user, or the brain to fix)."""


@dataclass(frozen=True, slots=True)
class Input:
    name: str
    #: What the user gives, in plain words: "the product pages to compare (their links)".
    about: str
    #: A list (a fan-out step runs once per item).
    many: bool = False
    #: Used when the user gives nothing; None: the input is required.
    default: str | None = None
    max_items: int = 8

    @property
    def required(self) -> bool:
        return self.default is None


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    #: What the user sees: "Read {item}" (only ``{item}`` and ``{input.…}`` are filled in).
    title: str
    #: The helper's brief, as a template.
    brief: str
    #: The only tools the step may use; every other call is refused.
    tools: tuple[str, ...] = ()
    #: Tools that always wait for the user's approval in this step (beyond each tool's own rule).
    ask: tuple[str, ...] = ()
    #: A list input: the step runs once per item, as that many helpers.
    each: str | None = None
    #: Model replies the step's turn may take.
    max_steps: int = 10
    #: Items of a fan-out step that may work at once (the helpers' own cap applies too).
    parallel: int = 3


@dataclass(frozen=True, slots=True)
class Workflow:
    name: str
    title: str
    #: What it does, in one plain sentence.
    about: str
    version: int
    inputs: tuple[Input, ...]
    steps: tuple[Step, ...]
    #: The account its steps read through, and how to connect it: ("Gmail", "/login google read").
    needs: tuple[str, str] | None = None
    _fingerprint: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        check(self)
        object.__setattr__(self, "_fingerprint", _digest(self.spec()))

    @property
    def fingerprint(self) -> str:
        """A hash of the whole definition: the same plan has the same fingerprint."""
        return self._fingerprint

    def spec(self) -> dict[str, Any]:
        """The definition as plain data: stored with every run, and what the fingerprint hashes."""
        return {
            "name": self.name,
            "title": self.title,
            "about": self.about,
            "version": self.version,
            "needs": list(self.needs) if self.needs else None,
            "inputs": [asdict(i) for i in self.inputs],
            "steps": [
                {**asdict(s), "tools": list(s.tools), "ask": list(s.ask)} for s in self.steps
            ],
        }

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "Workflow":
        """The definition a run stored: a resumed run follows the plan it started with."""
        needs = spec.get("needs")
        return cls(
            name=spec["name"],
            title=spec["title"],
            about=spec["about"],
            version=spec["version"],
            needs=(needs[0], needs[1]) if needs else None,
            inputs=tuple(Input(**i) for i in spec["inputs"]),
            steps=tuple(
                Step(**{**s, "tools": tuple(s["tools"]), "ask": tuple(s["ask"])})
                for s in spec["steps"]
            ),
        )

    def tools(self) -> list[str]:
        """Every tool its steps may use, in order."""
        return list(dict.fromkeys(t for s in self.steps for t in s.tools))

    def take(self, given: Mapping[str, object]) -> Inputs:
        """The inputs, checked and tidied (lists split, trimmed, without repeats), in order."""
        known = {i.name for i in self.inputs}
        unknown = [k for k in given if k not in known]
        if unknown:
            msg = f"“{unknown[0]}” is not an input of “{self.title}”. Its inputs: {self.usage()}."
            raise WorkflowInputError(msg)
        return {i.name: _value(i, given.get(i.name)) for i in self.inputs}

    def take_text(self, text: str) -> Inputs:
        """The inputs from one line, as a command gives them: each input in order, separated by
        “;”. A list input's items are separated by spaces or commas."""
        parts = [p.strip() for p in text.split(";")] if text.strip() else []
        if len(parts) > len(self.inputs):
            msg = f"Too many parts: “{self.title}” takes {self.usage()}, separated by “;”."
            raise WorkflowInputError(msg)
        return self.take({i.name: p for i, p in zip(self.inputs, parts, strict=False) if p})

    def usage(self) -> str:
        """Its inputs in words: "the product pages to compare (a list); what matters (optional)"."""
        return "; ".join(
            i.about + (" (a list)" if i.many else "") + ("" if i.required else " (optional)")
            for i in self.inputs
        )


def _value(spec: Input, value: object) -> str | list[str]:
    if spec.many:
        raw = value if isinstance(value, list) else SPLIT.split(str(value or ""))
        items = list(dict.fromkeys(str(v).strip() for v in raw if str(v).strip()))
        if not items and spec.default is not None:
            items = [spec.default]
        if not items:
            msg = f"“{spec.name}” is needed: {spec.about}."
            raise WorkflowInputError(msg)
        if len(items) > spec.max_items:
            msg = f"“{spec.name}” takes at most {spec.max_items} items; {len(items)} were given."
            raise WorkflowInputError(msg)
        if any(len(i) > MAX_ITEM_CHARS for i in items):
            msg = f"An item of “{spec.name}” is too long (at most {MAX_ITEM_CHARS} characters)."
            raise WorkflowInputError(msg)
        return items
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value or "")
    text = text.strip() or (spec.default or "")
    if not text:
        msg = f"“{spec.name}” is needed: {spec.about}."
        raise WorkflowInputError(msg)
    if len(text) > MAX_TEXT_CHARS:
        msg = f"“{spec.name}” is too long (at most {MAX_TEXT_CHARS} characters)."
        raise WorkflowInputError(msg)
    return text


@dataclass(frozen=True, slots=True)
class Output:
    """What an earlier step gave back, as a later step's brief shows it."""

    item: str | None
    status: str
    text: str | None


def render(
    template: str,
    *,
    flow: Workflow,
    inputs: Mapping[str, str | list[str]],
    item: str | None = None,
    outputs: Mapping[str, Sequence[Output]] | None = None,
) -> str:
    """Fill in a template in one pass: what is filled in is never read as a template again."""
    done = outputs or {}

    def fill(match: re.Match[str]) -> str:
        slot = match.group(1)
        if slot == "item":
            return item or ""
        kind, _, name = slot.partition(".")
        if kind == "input":
            value = inputs[name]
            return "\n".join(f"- {v}" for v in value) if isinstance(value, list) else value
        step = next(s for s in flow.steps if s.name == name)
        return _outputs(name, step.each is not None, done.get(name, ()))

    return SLOT.sub(fill, template)


def title_of(step: Step, *, flow: Workflow, inputs: Inputs, item: str | None) -> str:
    """A step's title for people: an address without its scheme, kept short."""
    shown = re.sub(r"^https?://(www\.)?", "", item).rstrip("/") if item else None
    return render(step.title, flow=flow, inputs=inputs, item=shown)


def _outputs(name: str, fanned: bool, outputs: Sequence[Output]) -> str:
    """An earlier step's output: fenced, since it came from another agent's work (§12.2)."""
    if not fanned:
        found = outputs[0] if outputs else None
        if found is None or found.status != "done":
            return "(that step gave nothing back)"
        return fence(found.text or "", source=f"workflow step {name}")
    parts = []
    for n, out in enumerate(outputs, start=1):
        if out.status == "done":
            parts.append(f"{n}. {out.item}\n" + fence(out.text or "", source=f"step {name} #{n}"))
        else:
            parts.append(f"{n}. {out.item}: this one could not be done ({out.status}).")
    return "\n\n".join(parts)


def check(flow: Workflow) -> None:
    """A definition that cannot run is refused when it is made, never mid-run."""
    if not NAME.match(flow.name):
        _bad(flow, f"its name {flow.name!r} must be lowercase words joined by “-”")
    if flow.version < 1 or not flow.steps:
        _bad(flow, "it needs a version from 1 and at least one step")
    inputs = {i.name: i for i in flow.inputs}
    if len(inputs) != len(flow.inputs) or not all(KEY.match(n) for n in inputs):
        _bad(flow, "its inputs need distinct names in lowercase")
    seen: list[str] = []
    for step in flow.steps:
        if not KEY.match(step.name) or step.name in seen:
            _bad(flow, f"step {step.name!r} needs a distinct name in lowercase")
        if step.each is not None and not (step.each in inputs and inputs[step.each].many):
            _bad(flow, f"step {step.name!r} fans out over {step.each!r}, which is not a list")
        _check_slots(flow, step, step.brief, inputs, seen, title=False)
        _check_slots(flow, step, step.title, inputs, seen, title=True)
        if FORBIDDEN & set(step.tools):
            _bad(flow, f"step {step.name!r} may not hand work on or start workflows")
        if not set(step.ask) <= set(step.tools):
            _bad(flow, f"step {step.name!r} asks first for tools it does not have")
        if not (1 <= step.max_steps <= 30 and 1 <= step.parallel <= 4):  # noqa: PLR2004
            _bad(flow, f"step {step.name!r} needs 1-30 replies and 1-4 items at once")
        seen.append(step.name)


def _check_slots(  # noqa: PLR0913 - what a template is checked against
    flow: Workflow,
    step: Step,
    template: str,
    inputs: Mapping[str, Input],
    earlier: Sequence[str],
    *,
    title: bool,
) -> None:
    for slot in SLOT.findall(template):
        kind, _, name = slot.partition(".")
        if slot == "item" and step.each is not None:
            continue
        if kind == "input" and name in inputs:
            continue
        if kind == "steps" and name in earlier and not title:
            continue
        _bad(flow, f"step {step.name!r} has {{{slot}}}, which it cannot fill in")


def _bad(flow: Workflow, why: str) -> None:
    msg = f"Workflow {flow.name!r}: {why}."
    raise ValueError(msg)


def _digest(spec: Mapping[str, Any]) -> str:
    blob = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
