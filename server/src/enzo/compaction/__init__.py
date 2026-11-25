"""Context management: budgets, compaction and stored long texts (spec §1.3.5, §5; ADR 0024)."""

from enzo.compaction.budget import Budget
from enzo.compaction.summary import Correction, Note, StoredRef, Summary
from enzo.compaction.view import Compaction, build_view
from enzo.compaction.window import Compacted, ContextOverflowError, ContextWindow, Fit

__all__ = [
    "Budget",
    "Compacted",
    "Compaction",
    "ContextOverflowError",
    "ContextWindow",
    "Correction",
    "Fit",
    "Note",
    "StoredRef",
    "Summary",
    "build_view",
]
