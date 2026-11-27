import { describe, expect, it, mock } from "bun:test";
import type { GatewayEvent, StoredMessage } from "@enzo/sdk";
import { act } from "react";
import { helperLine } from "./commands.ts";
import { CONVERSATION, enter, fakeClient, helperOf, quiet, screen, until } from "./testing.tsx";

function arriving(...events: GatewayEvent[]) {
  return async function* (signal?: AbortSignal): AsyncGenerator<GatewayEvent> {
    yield* events;
    yield* quiet(signal);
  };
}

const brief: StoredMessage = {
  role: "user",
  origin: "brief",
  parts: [
    { type: "text", text: "Research these 5 laptops and compare them" },
    { type: "text", text: "[sent …] This chat is a helper's" },
  ],
};

describe("helpers in the terminal (spec §9.5)", () => {
  it("/helpers lists them with how each is doing, and opens one's chat", async () => {
    const opened = mock(async (id: string) => ({ ...CONVERSATION, id }));
    const client = fakeClient({
      helpers: async () => [
        helperOf(3, { status: "waiting", waiting: "Post a note: battery" }),
        helperOf(2, { title: "Find flights", status: "done", steps: 1, cost_usd: null }),
      ],
      conversation: opened,
      messages: async (id) => (id === "h3" ? [brief] : []),
    });
    await using s = await screen(client);
    await enter(s, "/helpers");
    const list = await until(s, "#3 Compare 5 laptops");
    expect(list).toContain("waits for your approval: Post a note");
    expect(helperLine(helperOf(3, { status: "waiting", waiting: "Post a note: battery" }))).toBe(
      "waits for your approval: Post a note: battery · 4 steps · $0.02",
    );
    expect(list).toContain("#2 Find flights");
    expect(list).toContain("finished · 1 step · cost unknown");
    await act(async () => s.mockInput.pressEnter());
    const chat = await until(s, "Helper's brief: Research these 5 laptops");
    expect(opened).toHaveBeenCalledWith("h3");
    expect(chat).not.toContain("This chat is a helper's"); // the brain's note stays out of sight
  });

  it("/helpers stop stops the one picked", async () => {
    const stopHelper = mock(async (id: number) => ({
      stopped: true,
      helper: helperOf(id, { status: "stopped" }),
    }));
    const client = fakeClient({
      helpers: async () => [helperOf(3), helperOf(2, { status: "done" })],
      stopHelper,
    });
    await using s = await screen(client);
    await enter(s, "/helpers stop");
    const list = await until(s, "Stop which helper?");
    expect(list).not.toContain("#2"); // only helpers at work can be stopped
    await act(async () => s.mockInput.pressEnter());
    await until(s, "Stopped helper #3 “Compare 5 laptops”. Its work stopped.");
    expect(stopHelper).toHaveBeenCalledWith(3);
  });

  it("says so when there are none", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/helpers");
    await until(s, "No helpers yet.");
    await enter(s, "/helpers stop");
    await until(s, "No helper is working now.");
  });

  it("a helper's news shows in the chat on screen", async () => {
    const events = arriving(
      {
        kind: "helper",
        helper: 3,
        status: "waiting",
        words: "Helper #3 “Compare 5 laptops” waits for your approval: Post a note.",
        chat_id: "h3",
        conversation_id: "elsewhere",
      },
      { kind: "helper", helper: 3, status: "working", words: null, conversation_id: "x" },
    );
    await using s = await screen(fakeClient({ events }));
    await until(s, "waits for your approval: Post a note.");
  });

  it("a chat a helper changed is reopened in place", async () => {
    let shown: StoredMessage[] = [brief];
    const client = fakeClient({
      conversation: async () => ({ ...CONVERSATION, id: "h3" }),
      messages: async () => shown,
      events: async function* (signal?: AbortSignal) {
        await new Promise((r) => setTimeout(r, 300));
        shown = [brief, { role: "assistant", parts: [{ type: "text", text: "Report: B wins." }] }];
        yield {
          kind: "helper",
          helper: 3,
          status: "working",
          words: null,
          chat_id: "h3",
          conversation_id: "c1",
        };
        yield* quiet(signal);
      },
    });
    await using s = await screen(client, { start: "last", ready: "Helper's brief" });
    await until(s, "Report: B wins.");
  });
});
