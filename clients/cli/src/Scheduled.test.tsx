import { describe, expect, it, mock } from "bun:test";
import type { GatewayEvent, Scheduled, StoredMessage } from "@enzo/sdk";
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

const MOM: Scheduled = {
  id: 3,
  kind: "reminder",
  text: "Call mom",
  next_at: "2026-11-21T17:00:00Z",
  next: "Sat 21 Nov 09:00",
  schedule: "once, Sat 21 Nov 2026 at 09:00",
  timezone: "America/Los_Angeles",
  repeat: "once",
  ends_on: null,
  runs: 0,
  stop_when: null,
  conversation_id: "c1",
  chat_id: null,
};
const STANDUP: Scheduled = {
  ...MOM,
  id: 4,
  text: "Standup",
  next: "Mon 23 Nov 09:30",
  schedule: "every weekday at 09:30",
  repeat: "weekdays",
};

function arriving(...events: GatewayEvent[]) {
  return async function* (signal?: AbortSignal): AsyncGenerator<GatewayEvent> {
    yield* events;
    yield* quiet(signal);
  };
}

const EVENT = {
  kind: "reminder" as const,
  reminder: 3,
  text: "Call mom",
  late: false,
  conversation_id: "c1",
  next: null,
};

describe("a scheduled task reporting (spec §9.3)", () => {
  const report = (outcome: "reported" | "quiet" | "finished", words: string): GatewayEvent => ({
    kind: "task",
    task: 9,
    outcome,
    words,
    chat_id: "t9",
    conversation_id: "c1",
    stopped: outcome === "finished",
  });

  it("a report shows in the chat on screen; a quiet run does not", async () => {
    const events = arriving(
      report("quiet", "Table hunt: Not yet: nothing free."),
      report("reported", "Weather: Sunny, 21°C."),
    );
    await using s = await screen(fakeClient({ events }));
    const frame = await until(s, "⏰ Weather: Sunny, 21°C.");
    expect(frame).not.toContain("Not yet");
  });

  it("a chat shows a run as a run, and its report where it arrived", async () => {
    const own: StoredMessage[] = [
      {
        role: "user",
        origin: "schedule",
        parts: [
          { type: "text", text: "Check the weather in Austin" },
          { type: "text", text: "[sent …] Scheduled task #9, run 1" },
        ],
      },
      { role: "assistant", parts: [{ type: "text", text: "Sunny, 21°C." }] },
    ];
    const client = fakeClient({
      conversation: async () => ({ ...CONVERSATION, id: "t9" }),
      messages: async () => own,
    });
    await using s = await screen(client, { start: "last", ready: "Sunny, 21°C." });
    const frame = s.captureCharFrame();
    expect(frame).toContain("⏰ Scheduled run: Check the weather in Austin");
    expect(frame).not.toContain("Scheduled task #9"); // the brain's note stays out of sight
    expect(frame).not.toContain("you ·"); // not drawn as the user's own message
  });
});

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
      yield { ...EVENT, words: "Call mom" };
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
      kind: "reminder" as const,
      task: null,
      text: "Call mom",
      due_at: "2026-11-21T17:00:00Z",
      delivered_at: "2026-11-21T17:00:01Z",
      due: "Sat 09:00",
      after: 2,
      outcome: "delivered" as const,
      chat_id: null,
    };
    const client = fakeClient({
      conversation: async () => ({ ...CONVERSATION, id: "c1" }),
      messages: async () => messages,
      conversationScheduled: async () => [
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

describe("/scheduled and /cancel", () => {
  it("/reminders lists them soonest first, each with its schedule", async () => {
    await using s = await screen(fakeClient({ scheduled: async () => [MOM, STANDUP] }));
    await enter(s, "/reminders"); // still works: an alias of /scheduled
    const card = await until(s, "• Mon 23 Nov 09:30 · Standup");
    expect(card).toContain("• Sat 21 Nov 09:00 · Call mom");
    expect(card).toContain("once, Sat 21 Nov 2026 at 09:00 · #3");
    expect(card).toContain("every weekday at 09:30 · #4");
    expect(card).toContain("To change one, just tell Enzo. /cancel removes one.");
    expect(card.indexOf("Call mom")).toBeLessThan(card.indexOf("Standup"));
  });

  it("/scheduled with none says how to set one", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/scheduled");
    await until(s, "Nothing scheduled yet. Ask Enzo, like “remind me Friday at 9 to call mom” or");
  });

  it("/scheduled shows reminders and tasks apart, a watch with its end", async () => {
    const weather: Scheduled = {
      ...MOM,
      id: 9,
      kind: "task",
      text: "Look for a table for two on Friday",
      next: "Sat 21 Nov 10:00",
      schedule: "every hour, at most 24 runs",
      repeat: "hourly",
      stop_when: "a table is free",
    };
    await using s = await screen(fakeClient({ scheduled: async () => [MOM, weather] }), {
      height: 40,
    });
    await enter(s, "/tasks");
    const card = await until(s, "Scheduled tasks");
    expect(card.indexOf("Reminders")).toBeLessThan(card.indexOf("Scheduled tasks"));
    expect(card).toContain("• Sat 21 Nov 10:00 · Look for a table for two on Friday");
    expect(card).toContain("every hour, at most 24 runs, until a table is free · #9");
  });

  it("/cancel asks first; keeping it is the first choice", async () => {
    const cancelScheduled = mock(async (_id: number) => {});
    await using s = await screen(
      fakeClient({ scheduled: async () => [MOM, STANDUP], cancelScheduled }),
    );
    await enter(s, "/cancel");
    const list = await until(s, "Cancel which one?");
    expect(list).toContain("Sat 21 Nov 09:00 · Call mom");
    await press(s, "RETURN");
    await until(s, "Cancel this?");
    await press(s, "RETURN"); // keep it
    await gone(s, "Cancel this?");
    expect(cancelScheduled).not.toHaveBeenCalled();

    await enter(s, "/cancel");
    await until(s, "Cancel which one?");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Yes, cancel “Standup”");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Cancelled “Standup”.");
    expect(cancelScheduled).toHaveBeenCalledWith(4);
  });

  it("says when there is nothing to cancel, or no such reminder", async () => {
    const cancelScheduled = mock(async (_id: number) => {});
    await using s = await screen(fakeClient({ cancelScheduled }));
    await enter(s, "/cancel");
    await until(s, "Nothing scheduled to cancel.");
    await enter(s, "/cancel 9");
    await until(s, "There is nothing scheduled with #9. /scheduled lists them.");
    await enter(s, "/cancel soon");
    await until(s, "Usage: /cancel");
    await enter(s, "/cancel confirm");
    const frame = await settle(s, 200);
    expect(frame).not.toContain("/cancel confirm");
    expect(cancelScheduled).not.toHaveBeenCalled();
  });
});
