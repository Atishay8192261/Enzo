import type { StoredMessage } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import { initialState, NO_USAGE, reduce, SUMMARIZED, toItems } from "./chat.ts";

const ready = reduce(initialState, {
  type: "connected",
  conversationId: "c1",
  brains: [],
  brain: null,
  items: [],
});

describe("older messages summarized (spec §5.2)", () => {
  it("says so in a plain line before the reply, live", () => {
    let state = reduce(ready, { type: "user_sent", text: "what did we decide?" });
    state = reduce(state, {
      type: "event",
      event: { type: "compacted", text: SUMMARIZED, turns: 40, ok: true },
    });
    state = reduce(state, { type: "event", event: { type: "text", text: "Lisbon." } });
    state = reduce(state, {
      type: "event",
      event: {
        type: "done",
        stop: "final_answer",
        text: "Lisbon.",
        steps: 1,
        cost_usd: null,
        brain: null,
        model: null,
        failure: null,
        plan: false,
        usage: NO_USAGE,
      },
    });
    expect(state.items.map(({ role, text }) => ({ role, text }))).toEqual([
      { role: "user", text: "what did we decide?" },
      { role: "notice", text: SUMMARIZED },
      { role: "assistant", text: "Lisbon." },
    ]);
  });

  it("a reopened chat shows the line where it happened, and every message is still there", () => {
    const messages: StoredMessage[] = [
      { role: "user", parts: [{ type: "text", text: "first" }] },
      { role: "assistant", parts: [{ type: "text", text: "one" }] },
      { role: "user", parts: [{ type: "text", text: "second" }] },
      { role: "assistant", parts: [{ type: "text", text: "two" }], summarized: 1 },
    ];
    expect(toItems(messages).map(({ role, text }) => `${role}: ${text}`)).toEqual([
      "user: first",
      "assistant: one",
      "user: second",
      `notice: ${SUMMARIZED}`,
      "assistant: two",
    ]);
  });
});
