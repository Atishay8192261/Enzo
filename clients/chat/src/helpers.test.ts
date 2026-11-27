import type { GatewayEvent, HelperNotice, StoredMessage } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import { eventText, initialState, reduce, toItems } from "./chat.ts";

const user = (text: string, origin?: StoredMessage["origin"]): StoredMessage => ({
  role: "user",
  parts: [
    { type: "text", text },
    { type: "text", text: "[sent …]" },
  ],
  ...(origin ? { origin } : {}),
});
const reply = (text: string): StoredMessage => ({
  role: "assistant",
  parts: [{ type: "text", text }],
});

const notice = (after: number, text: string): HelperNotice => ({
  helper: 3,
  kind: "done",
  text,
  after,
  at: "2026-11-27T10:00:00Z",
  chat_id: "h3",
});

describe("helpers in a chat (spec §9.5)", () => {
  it("shows each line where it arrived, and Enzo's review of a report without its handoff", () => {
    const messages = [
      user("Compare five laptops"),
      reply("A helper is on it."),
      user("Reports came back from helper #3.", "handoff"),
      reply("The helper found that B is best."),
    ];
    const items = toItems(messages, null, [], [notice(2, "Helper #3 “Compare” finished.")]);
    expect(items.map((i) => [i.role, i.text])).toEqual([
      ["user", "Compare five laptops"],
      ["assistant", "A helper is on it."],
      ["notice", "Helper #3 “Compare” finished."],
      ["assistant", "The helper found that B is best."],
    ]);
  });

  it("starts a helper's own chat with its brief, not as something the user typed", () => {
    const items = toItems([user("Research 5 laptops\n\nLimits: none", "brief"), reply("Report.")]);
    expect(items.map((i) => [i.role, i.text])).toEqual([
      ["notice", "Helper's brief: Research 5 laptops\n\nLimits: none"],
      ["assistant", "Report."],
    ]);
  });

  it("says a helper's news live, but only what has words", () => {
    const base = { kind: "helper", helper: 3, conversation_id: "c1", chat_id: "h3" } as const;
    const said: GatewayEvent = { ...base, status: "waiting", words: "Helper #3 waits." };
    const step: GatewayEvent = { ...base, status: "working", words: null };
    expect(eventText(said)).toBe("Helper #3 waits.");
    expect(eventText(step)).toBeNull();
  });

  it("ignores a stream's events once another chat is on screen", () => {
    const open = { ...initialState, status: "ready" as const, conversationId: "main" };
    const text = { type: "text", text: "from the helper's chat" } as const;
    const elsewhere = reduce(open, { type: "event", event: text, chat: "h3" });
    expect(elsewhere).toBe(open);
    const here = reduce(open, { type: "event", event: text, chat: "main" });
    expect(here.draft).toBe("from the helper's chat");
  });
});
