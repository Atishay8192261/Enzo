"""Each brain's token budget, from its context window and the configured split (spec §5.2)."""

from dataclasses import dataclass

from enzo.brain.config import BrainConfig, ContextConfig


@dataclass(frozen=True, slots=True)
class Budget:
    """What a prompt may use with one brain, in tokens."""

    #: The window it was derived from; ``confirmed`` when the provider publishes it.
    window: int
    confirmed: bool
    #: Reserved for the reply (the call's ``max_output_tokens``).
    reply: int
    #: A prompt above this is never sent, at any step of a turn.
    limit: int
    #: At the start of a turn, a prompt above this has its older turns summarized first.
    compact_at: int
    #: Tokens of the newest whole turns kept word for word when older ones are summarized.
    keep: int

    @classmethod
    def of(cls, brain: BrainConfig, ctx: ContextConfig, *, reply: int) -> "Budget":
        window = brain.context_window or ctx.default_window
        if brain.max_output_tokens:
            reply = min(reply, brain.max_output_tokens)
        limit = int(window * (1 - ctx.margin)) - reply
        if limit <= 0:
            msg = f"a {window}-token window leaves no room for a {reply}-token reply"
            raise ValueError(msg)
        # The task share stays free at a turn's start; and a prompt that would reach the brain's
        # long-context price step (e.g. 2x above 272K input tokens) is compacted before it does.
        compact_at = min(ctx.compact_at, int(limit * (1 - ctx.task_share)))
        long = brain.pricing.long_context if brain.pricing else None
        if long is not None:
            compact_at = min(compact_at, long.from_input_tokens - 1)
        return cls(
            window=window,
            confirmed=brain.context_window is not None,
            reply=reply,
            limit=limit,
            compact_at=compact_at,
            keep=min(ctx.keep_recent, compact_at // 3),
        )

    def tighter(self, other: "Budget") -> "Budget":
        """A budget both brains can serve: a fallback must fit the same prompt."""
        return Budget(
            window=min(self.window, other.window),
            confirmed=self.confirmed and other.confirmed,
            reply=min(self.reply, other.reply),
            limit=min(self.limit, other.limit),
            compact_at=min(self.compact_at, other.compact_at),
            keep=min(self.keep, other.keep),
        )
