import type { ScheduledNotice, StoredMessage } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import { noticeText, toItems } from "./chat.ts";

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
