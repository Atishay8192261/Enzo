import type { ScheduledNotice, StoredMessage } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import { eventText, noticeText, scheduledItem, toItems } from "./chat.ts";

const base: ScheduledNotice = {
  kind: "task",
  task: "Look for a table on Friday",
  text: "A table is free at 7pm.",
  due_at: "2026-11-21T17:00:00Z",
  delivered_at: "2026-11-21T17:00:05Z",
  late: false,
  after: 2,
  outcome: "reported",
  due: "Sat 09:00",
  chat_id: "t1",
};

describe("scheduled notices in a chat's timeline (spec §9.3)", () => {
  it("words each outcome, and leaves quiet runs out", () => {
    expect(noticeText(base)).toBe("Look for a table on Friday: A table is free at 7pm.");
    expect(noticeText({ ...base, outcome: "finished" })).toBe(
      "Look for a table on Friday: A table is free at 7pm. Done: it stops now.",
    );
    expect(noticeText({ ...base, outcome: "needs_approval" })).toBe(
      "Look for a table on Friday: it waits for your approval in its own chat.",
    );
    expect(noticeText({ ...base, outcome: "failed", text: "No model account." })).toBe(
      "Look for a table on Friday: the run failed. No model account.",
    );
    expect(noticeText({ ...base, outcome: "quiet", text: "Not yet." })).toBeNull();
    const reminder = { ...base, kind: "reminder" as const, task: null, text: "Call mom" };
    expect(noticeText(reminder)).toBe("Reminder: Call mom");
    expect(noticeText({ ...reminder, late: true })).toBe(
      "Reminder: Call mom (late: due Sat 09:00, Enzo wasn't running)",
    );
  });

  it("places them where they arrived and skips the quiet ones", () => {
    const messages: StoredMessage[] = [
      { role: "user", parts: [{ type: "text", text: "find me a table" }] },
      { role: "assistant", parts: [{ type: "text", text: "Watching for one." }] },
    ];
    const items = toItems(messages, null, [
      { ...base, outcome: "quiet", text: "Not yet.", after: 2 },
      { ...base, after: 2 },
    ]);
    expect(items.map((i) => i.text)).toEqual([
      "find me a table",
      "Watching for one.",
      "⏰ Look for a table on Friday: A table is free at 7pm.",
    ]);
  });
});

describe("watches in a chat (spec §9.4)", () => {
  const watch: ScheduledNotice = {
    ...base,
    kind: "watch",
    task: "the kettle's price",
    text: "the kettle's price: now $39.00, under your $40. That was its last wake.",
  };

  it("shows a watch's report or stop as the gateway words it, with its own sign", () => {
    expect(noticeText(watch)).toBe(
      "👀 the kettle's price: now $39.00, under your $40. That was its last wake.",
    );
    const stopped = { ...watch, outcome: "stopped" as const, text: "Enzo stopped watching x." };
    expect(noticeText(stopped)).toBe("👀 Enzo stopped watching x.");
    const live = eventText({
      kind: "watch",
      watch: 3,
      outcome: "reported",
      words: "the kettle's price: now $39.00.",
      chat_id: "w1",
      conversation_id: "c1",
      stopped: true,
    });
    expect(live).toBe("👀 the kettle's price: now $39.00.");
    expect(scheduledItem(live ?? "").text).toBe(live); // never a clock on top
  });

  it("shows a wake's message as the watch's, not as the user typing", () => {
    const messages: StoredMessage[] = [
      {
        role: "user",
        origin: "watch",
        parts: [
          { type: "text", text: "Tell me the new price." },
          { type: "text", text: "[sent …] Watch #3 saw a change" },
        ],
      },
      { role: "assistant", parts: [{ type: "text", text: "It is $39.00 now." }] },
    ];
    const items = toItems(messages, null, [watch]);
    expect(items.map((i) => [i.role, i.text])).toEqual([
      ["notice", "👀 Watch: Tell me the new price."],
      ["assistant", "It is $39.00 now."],
      ["notice", "👀 the kettle's price: now $39.00, under your $40. That was its last wake."],
    ]);
  });
});
