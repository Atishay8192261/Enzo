import { describe, expect, it, mock } from "bun:test";
import type { Goal, GoalEvidence } from "@enzo/sdk";
import { enter, fakeClient, screen, until } from "./testing.tsx";

const SAID: GoalEvidence = {
  evidence: "The user said they got their passport photos.",
  basis: "user_confirmed",
  source: null,
  happened_on: null,
  recorded_at: "2026-11-24T10:00:00Z",
  said: "you confirmed, 24 Nov 2026, in “Passport”",
};
const PASSPORT: Goal = {
  id: 3,
  title: "Renew my passport",
  status: "open",
  due_on: "2026-12-15",
  due: "Tue 15 Dec 2026",
  folder: "~/Enzo/passport",
  created_at: "2026-11-24T10:00:00Z",
  closed_at: null,
  chat: "Passport",
  conversation_id: "c1",
  steps: [
    { number: 1, text: "Get photos", done: true, done_at: "2026-11-24T10:00:00Z", evidence: SAID },
    { number: 2, text: "Fill in the form", done: false, done_at: null, evidence: null },
  ],
  check_ins: [
    {
      id: 7,
      kind: "reminder",
      text: "Passport",
      schedule: "every Monday at 09:00",
      next: "Mon 30 Nov 09:00",
    },
  ],
  closing: null,
};
const BREAD: Goal = {
  ...PASSPORT,
  id: 1,
  title: "Bake bread",
  status: "done",
  due: null,
  due_on: null,
  folder: null,
  steps: [],
  check_ins: [],
  closed_at: "2026-11-25T10:00:00Z",
  closing: {
    ...SAID,
    evidence: "The user said they baked their first loaf.",
    said: "you confirmed, 25 Nov 2026",
  },
};

describe("/goals (spec §6.4)", () => {
  it("lists open goals with their steps, the evidence for done ones, check-ins and folder", async () => {
    const goals = mock(async (_which?: "open" | "closed") => ({
      goals: [PASSPORT],
      file: "~/Enzo/Goals.md",
    }));
    await using s = await screen(fakeClient({ goals }), { height: 40 });
    await enter(s, "/goals");
    const card = await until(s, "Open goals");
    expect(card).toContain("• Renew my passport · due Tue 15 Dec 2026 · #3");
    expect(card).toContain("✓ Get photos (you confirmed, 24 Nov 2026, in “Passport”)");
    expect(card).toContain("○ Fill in the form");
    expect(card).toContain("Check-in: every Monday at 09:00 (next Mon 30 Nov 09:00)");
    expect(card).toContain("In ~/Enzo/passport");
    expect(card).toContain("To change one, just tell Enzo. /goals done lists finished ones.");
    expect(card).toContain("A read-only copy:");
    expect(card.indexOf("Get photos")).toBeLessThan(card.indexOf("Fill in the form"));
    expect(goals).toHaveBeenCalledWith("open");
  });

  it("/goals done lists finished ones with why each was closed", async () => {
    const goals = mock(async (_which?: "open" | "closed") => ({ goals: [BREAD], file: null }));
    await using s = await screen(fakeClient({ goals }), { height: 40 });
    await enter(s, "/goals done");
    const card = await until(s, "Finished goals");
    expect(card).toContain("• Bake bread · done · #1");
    expect(card).toContain(
      "you confirmed, 25 Nov 2026: The user said they baked their first loaf.",
    );
    expect(card).not.toContain("○");
    expect(goals).toHaveBeenCalledWith("closed");
  });

  it("says how to start one when there are none", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/goals");
    await until(s, "No open goals. Tell Enzo what you are working toward");
    await enter(s, "/goals done");
    await until(s, "No finished goals yet.");
    await enter(s, "/goals everything");
    await until(s, "Usage: /goals [done]");
  });
});
