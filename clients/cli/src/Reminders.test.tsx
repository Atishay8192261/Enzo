import { describe, expect, it, mock } from "bun:test";
import type { GatewayEvent, StoredMessage, UpcomingReminder } from "@enzo/sdk";
import { act } from "react";
import {
  CONVERSATION,
  enter,
  fakeClient,
  gone,
  lines,
  press,
  quiet,
  screen,
  settle,
  until,
} from "./testing.tsx";

const MOM: UpcomingReminder = {
  id: 3,
  text: "Call mom",
  next_at: "2026-11-21T17:00:00Z",
  next: "Sat 21 Nov 09:00",
  schedule: "once, Sat 21 Nov 2026 at 09:00",
  timezone: "America/Los_Angeles",
  repeat: "once",
  ends_on: null,
  runs: 0,
  conversation_id: "c1",
};
const STANDUP: UpcomingReminder = {
  ...MOM,
  id: 4,
  text: "Standup",
  next: "Mon 23 Nov 09:30",
  schedule: "every weekday at 09:30",
  repeat: "weekdays",
};

function arriving(...events: Omit<GatewayEvent, "kind">[]) {
  return async function* (signal?: AbortSignal): AsyncGenerator<GatewayEvent> {
    for (const e of events) yield { kind: "reminder", ...e };
    yield* quiet(signal);
  };
}

const EVENT = { reminder: 3, text: "Call mom", late: false, conversation_id: "c1", next: null };

describe("a reminder arriving while Enzo is open (spec §9.3)", () => {
  it("shows in the chat on screen, marked late when it is", async () => {
    const events = arriving(
      { ...EVENT, words: "Call mom" },
      { ...EVENT, text: "Standup", words: "Standup (late: due Mon 09:30, Enzo wasn't running)" },
    );
    await using s = await screen(fakeClient({ events }));
    const frame = await until(s, "⏰ Reminder: Standup");
    expect(frame).toContain("⏰ Reminder: Call mom");
    expect(frame).toContain("(late: due Mon 09:30, Enzo wasn't running)");
  });

  it("listens again after the gateway restarts", async () => {
    let opened = 0;
    const events = async function* (signal?: AbortSignal): AsyncGenerator<GatewayEvent> {
      opened += 1;
      if (opened === 1) throw new Error("connection refused");
      yield { kind: "reminder", ...EVENT, words: "Call mom" };
      yield* quiet(signal);
    };
    await using s = await screen(fakeClient({ events }));
    await until(s, "⏰ Reminder: Call mom", 5000);
    expect(opened).toBe(2);
  });

  it("a chat reopened later shows its reminders where they arrived", async () => {
    const messages: StoredMessage[] = [
      { role: "user", parts: [{ type: "text", text: "remind me at 9 to call mom" }] },
      { role: "assistant", parts: [{ type: "text", text: "I'll remind you at 09:00." }] },
      { role: "user", parts: [{ type: "text", text: "thanks, what next?" }] },
      { role: "assistant", parts: [{ type: "text", text: "Lunch." }] },
    ];
    const delivered = {
      text: "Call mom",
      due_at: "2026-11-21T17:00:00Z",
      delivered_at: "2026-11-21T17:00:01Z",
      due: "Sat 09:00",
      after: 2,
    };
    const client = fakeClient({
      conversation: async () => ({ ...CONVERSATION, id: "c1" }),
      messages: async () => messages,
      conversationReminders: async () => [
        { ...delivered, late: false },
        { ...delivered, text: "Stretch", late: true, after: 4 },
      ],
    });
    await using s = await screen(client, { start: "last", ready: "Lunch.", height: 50 });
    const rows = lines(s.captureCharFrame());
    const at = (text: string) => rows.findIndex((r) => r.includes(text));
    expect(at("I'll remind you")).toBeLessThan(at("⏰ Reminder: Call mom"));
    expect(at("⏰ Reminder: Call mom")).toBeLessThan(at("thanks, what next?"));
    expect(at("Lunch.")).toBeLessThan(at("⏰ Reminder: Stretch (late: due Sat 09:00"));
  });
});

describe("/reminders and /cancel", () => {
  it("/reminders lists them soonest first, each with its schedule", async () => {
    await using s = await screen(fakeClient({ reminders: async () => [MOM, STANDUP] }));
    await enter(s, "/reminders");
    const card = await until(s, "• Mon 23 Nov 09:30 · Standup");
    expect(card).toContain("• Sat 21 Nov 09:00 · Call mom");
    expect(card).toContain("once, Sat 21 Nov 2026 at 09:00 · #3");
    expect(card).toContain("every weekday at 09:30 · #4");
    expect(card).toContain("To change one, just tell Enzo. /cancel removes one.");
    expect(card.indexOf("Call mom")).toBeLessThan(card.indexOf("Standup"));
  });

  it("/reminders with none says how to set one", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/reminders");
    await until(s, "No reminders yet. Ask Enzo, like “remind me Friday at 9 to call mom”.");
  });

  it("/cancel asks first; keeping it is the first choice", async () => {
    const cancelReminder = mock(async (_id: number) => {});
    await using s = await screen(
      fakeClient({ reminders: async () => [MOM, STANDUP], cancelReminder }),
    );
    await enter(s, "/cancel");
    const list = await until(s, "Cancel which reminder?");
    expect(list).toContain("Sat 21 Nov 09:00 · Call mom");
    await press(s, "RETURN");
    await until(s, "Cancel this reminder?");
    await press(s, "RETURN"); // keep it
    await gone(s, "Cancel this reminder?");
    expect(cancelReminder).not.toHaveBeenCalled();

    await enter(s, "/cancel");
    await until(s, "Cancel which reminder?");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Yes, cancel “Standup”");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Cancelled the reminder “Standup”.");
    expect(cancelReminder).toHaveBeenCalledWith(4);
  });

  it("says when there is nothing to cancel, or no such reminder", async () => {
    const cancelReminder = mock(async (_id: number) => {});
    await using s = await screen(fakeClient({ cancelReminder }));
    await enter(s, "/cancel");
    await until(s, "No reminders to cancel.");
    await enter(s, "/cancel 9");
    await until(s, "There is no reminder #9 to come. /reminders lists them.");
    await enter(s, "/cancel soon");
    await until(s, "Usage: /cancel");
    await enter(s, "/cancel confirm");
    const frame = await settle(s, 200);
    expect(frame).not.toContain("/cancel confirm");
    expect(cancelReminder).not.toHaveBeenCalled();
  });
});
