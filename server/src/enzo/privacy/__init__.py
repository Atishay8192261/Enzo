"""Discretion (spec §12.5, ADR 0029): what Enzo shares with others, not what it reads.

- ``detect``: private details in outgoing text, by pattern and by what Enzo knows is private.
- ``check``: the outbound check the agent loop runs before a call sends text out.
- ``rules``: the discretion rules the user stated, per chat or for every chat.
- ``tools``: the brain's tools to save and lift a rule, only from the user's own words.
"""

from enzo.privacy.check import Discretion, Verdict
from enzo.privacy.detect import RULE_KINDS, RuleKind
from enzo.privacy.rules import Rule, RuleRefusedError, Rules, rules_note
from enzo.privacy.tools import privacy_tools

__all__ = [
    "RULE_KINDS",
    "Discretion",
    "Rule",
    "RuleKind",
    "RuleRefusedError",
    "Rules",
    "Verdict",
    "privacy_tools",
    "rules_note",
]
