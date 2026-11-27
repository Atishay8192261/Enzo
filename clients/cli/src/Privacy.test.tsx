import { describe, expect, it, mock } from "bun:test";
import type { PrivacyRule } from "@enzo/sdk";
import { act } from "react";
import { enter, fakeClient, gone, press, screen, until } from "./testing.tsx";

const HEALTH: PrivacyRule = {
  id: 4,
  text: "Don't mention my health to anyone",
  kinds: ["health"],
  words: [],
  covers: "health",
  scope: "all",
  conversation_id: null,
  chat: null,
  quote: "don't mention my health to anyone",
  created_at: "2026-11-26T10:00:00Z",
};
const WORK: PrivacyRule = {
  ...HEALTH,
  id: 5,
  text: "In this chat never bring up work",
  kinds: [],
  words: ["work"],
  covers: "the words “work”",
  scope: "chat",
  conversation_id: "c1",
  quote: "in this chat never bring up work",
};

describe("/privacy (spec §12.5)", () => {
  it("lists the rules that hold here: where, what each covers, in the user's words", async () => {
    const privacyRules = mock(async (_chat?: string | null) => [HEALTH, WORK]);
    await using s = await screen(fakeClient({ privacyRules }), { height: 40 });
    await enter(s, "/privacy");
    const card = await until(s, "Enzo never shares these with anyone outside this chat:");
    expect(card).toContain("• Don't mention my health to anyone (every chat) · #4");
    expect(card).toContain("covers health · you said “don't mention my health to anyone”");
    expect(card).toContain("• In this chat never bring up work (this chat) · #5");
    expect(card).toContain("To change one, just tell Enzo. /privacy lift removes one.");
  });

  it("says how to set one when there is none", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/privacy");
    const card = await until(s, "No privacy rules yet.");
    expect(card).toContain("“don't mention my health to anyone”");
  });

  it("lifts a rule only after a yes; keeping it is the first choice", async () => {
    const removePrivacyRule = mock(async (_id: number) => {});
    const client = fakeClient({ privacyRules: async () => [HEALTH, WORK], removePrivacyRule });
    await using s = await screen(client);
    await enter(s, "/privacy lift");
    const list = await until(s, "Lift which privacy rule?");
    expect(list).toContain("In this chat never bring up work (this chat)");
    await press(s, "RETURN"); // the first: health
    await until(s, "Lift this rule?");
    await press(s, "RETURN"); // keep it
    await gone(s, "Lift this rule?");
    expect(removePrivacyRule).not.toHaveBeenCalled();

    await enter(s, "/privacy lift");
    await until(s, "Lift which privacy rule?");
    await act(async () => {
      s.mockInput.pressArrow("down"); // work
    });
    await press(s, "RETURN");
    await until(s, "Yes, lift “In this chat never bring up work”");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Lifted the rule “In this chat never bring up work”.");
    expect(removePrivacyRule).toHaveBeenCalledWith(5);
  });

  it("has nothing to lift when there are no rules", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/privacy lift");
    await until(s, "No privacy rules to lift.");
  });
});
