import { describe, expect, it } from "bun:test";
import { SUMMARIZED } from "@enzo/chat";
import type { StoredMessage } from "@enzo/sdk";
import { CONVERSATION, done, enter, events, fakeClient, lines, screen, until } from "./testing.tsx";

describe("a long chat whose older messages were summarized (spec §5.2)", () => {
  it("says so in one plain line, before the reply", async () => {
    const client = fakeClient({
      send: () =>
        events(
          { type: "compacted", text: SUMMARIZED, turns: 40, ok: true },
          { type: "text", text: "You chose Lisbon." },
          done({ text: "You chose Lisbon." }),
        ),
    });
    await using s = await screen(client, { height: 40 });
    await enter(s, "what did we decide at the start?");
    await until(s, "You chose Lisbon.");
    const rows = lines(s.captureCharFrame());
    const at = (text: string) => rows.findIndex((r) => r.includes(text));
    expect(at("Older messages were summarized")).toBeGreaterThan(at("what did we decide"));
    expect(at("Older messages were summarized")).toBeLessThan(at("You chose Lisbon."));
    expect(s.captureCharFrame()).not.toContain("compact");
  });

  it("a reopened chat shows the line where it happened, with every message still there", async () => {
    const messages: StoredMessage[] = [
      { role: "user", parts: [{ type: "text", text: "plan the trip" }] },
      { role: "assistant", parts: [{ type: "text", text: "Lisbon it is." }] },
      { role: "user", parts: [{ type: "text", text: "and the hotel?" }] },
      { role: "assistant", parts: [{ type: "text", text: "Booked." }], summarized: 1 },
    ];
    const client = fakeClient({
      conversation: async () => CONVERSATION,
      messages: async () => messages,
    });
    await using s = await screen(client, { start: "last", ready: "Booked.", height: 50 });
    const rows = lines(s.captureCharFrame());
    const at = (text: string) => rows.findIndex((r) => r.includes(text));
    expect(at("Lisbon it is.")).toBeGreaterThan(-1);
    expect(at("and the hotel?")).toBeLessThan(at("Older messages were summarized"));
    expect(at("Older messages were summarized")).toBeLessThan(at("Booked."));
  });
});
