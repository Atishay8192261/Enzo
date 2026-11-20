import { describe, expect, it, mock } from "bun:test";
import type { MemoryList, Remembered } from "@enzo/sdk";
import { act } from "react";
import { enter, fakeClient, gone, press, screen, settle, until } from "./testing.tsx";

const SEATS: Remembered = {
  id: 2,
  text: "Prefers aisle seats on flights.",
  kind: "preference",
  about: null,
  said_by: "user",
  happened_on: null,
  learned_at: "2026-11-20T10:00:00Z",
  chat: "Trip to NYC",
  conversation_id: "c-trip",
  source: "you said, 20 Nov 2026, in “Trip to NYC”",
};
const SISTER: Remembered = {
  ...SEATS,
  id: 3,
  text: "Sister, lives in Austin.",
  kind: "person",
  about: "Priya",
  said_by: "inferred",
  source: "Enzo inferred, 20 Nov 2026",
};
const KNOWN: MemoryList = { memories: [SEATS, SISTER], file: "~/Enzo/Memory.md" };

describe("/memory (spec §6.2, §15.2)", () => {
  it("lists what Enzo remembers by kind, each with where it came from", async () => {
    await using s = await screen(fakeClient({ memories: async () => KNOWN }), { height: 40 });
    await enter(s, "/memory");
    const card = await until(s, "Preferences");
    expect(card).toContain("• Prefers aisle seats on flights.");
    expect(card).toContain("you said, 20 Nov 2026, in “Trip to NYC” · #2");
    expect(card).toContain("People");
    expect(card).toContain("• Priya: Sister, lives in Austin.");
    expect(card).toContain("Enzo inferred, 20 Nov 2026 · #3"); // an inference is labelled so
    expect(card).toContain("To change one, just tell Enzo. /forget removes one.");
    const rows = card.split("\n").map((r) => r.replaceAll("│", "").trim());
    expect(rows).toContain("~/Enzo/Memory.md"); // whole, on its own line
    expect(card.indexOf("Preferences")).toBeLessThan(card.indexOf("People"));
  });

  it("says how to teach it when it remembers nothing yet", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/memory");
    const card = await until(
      s,
      "Enzo remembers nothing yet. Tell it something, like “remember I prefer",
    );
    expect(card).toContain("A readable copy:");
  });
});

describe("/forget", () => {
  it("asks first; keeping it is the first choice", async () => {
    const forgetMemory = mock(async (_id: number) => {});
    await using s = await screen(fakeClient({ memories: async () => KNOWN, forgetMemory }));
    await enter(s, "/forget");
    const list = await until(s, "Forget which memory?");
    expect(list).toContain("Prefers aisle seats on flights.");
    expect(list).toContain("Priya: Sister, lives in Austin.");
    await press(s, "RETURN"); // the newest: the seats
    await until(s, "Forget this?");
    await press(s, "RETURN"); // the first choice: keep it
    await gone(s, "Forget this?");
    expect(forgetMemory).not.toHaveBeenCalled();

    await enter(s, "/forget");
    await until(s, "Forget which memory?");
    await act(async () => {
      s.mockInput.pressArrow("down"); // the sister
    });
    await press(s, "RETURN");
    await until(s, "Yes, forget “Priya: Sister, lives in Austin.”");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Forgot “Priya: Sister, lives in Austin.”.");
    expect(forgetMemory).toHaveBeenCalledWith(3);
  });

  it("says when there is nothing to forget, or no such memory", async () => {
    const forgetMemory = mock(async (_id: number) => {});
    await using s = await screen(fakeClient({ forgetMemory }));
    await enter(s, "/forget");
    await until(s, "Enzo remembers nothing yet, so there is nothing to forget.");
    await enter(s, "/forget 9");
    await until(s, "Enzo has no memory #9. /memory lists them.");
    await enter(s, "/forget abc");
    await until(s, "Usage: /forget");
    await enter(s, "/forget confirm"); // nothing was being asked about: nothing happens
    const frame = await settle(s, 200);
    expect(frame).not.toContain("/forget confirm");
    expect(forgetMemory).not.toHaveBeenCalled();
  });
});
