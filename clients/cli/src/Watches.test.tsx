import { describe, expect, it, mock } from "bun:test";
import type { GatewayEvent, Watch, WatchCheck } from "@enzo/sdk";
import { act } from "react";
import { enter, fakeClient, gone, press, quiet, screen, until } from "./testing.tsx";

const KETTLE: Watch = {
  id: 3,
  what: "the kettle's price",
  status: "off",
  checks: "the text at “.price” on shop.example/kettle, every 10 minutes",
  change: "the number in it goes below 40",
  ends: "at most 1 wake",
  on_change: "Tell me the new price.",
  reads: "$59.99",
  read_at: "Fri 27 Nov 09:00",
  next_check: null,
  stop_reason: null,
  last_error: null,
  wakes: 0,
};

const CHECK: WatchCheck = {
  watch: KETTLE,
  reads: "$59.99",
  change: "the number in it goes below 40",
  checked_at: "Fri 27 Nov 09:01",
  token: "abc123",
};

describe("watches in the terminal (spec §9.4)", () => {
  it("/watches lists each watch with what it last read, and why a stopped one stopped", async () => {
    const stopped: Watch = {
      ...KETTLE,
      id: 4,
      what: "new email from the airline",
      status: "stopped",
      stop_reason: "rate_limited",
      last_error: "Gmail refused the request (HTTP 429)",
    };
    await using s = await screen(fakeClient({ watches: async () => [KETTLE, stopped] }), {
      height: 40,
    });
    await enter(s, "/watches");
    const card = await until(s, "• stopped · new email from the airline · #4");
    expect(card).toContain("• off · the kettle's price · #3");
    expect(card).toContain("Last read: $59.99 (Fri 27 Nov 09:00)");
    expect(card).toContain("Stopped: the site or service said too many requests");
    expect(card).toContain("/enable turns one on");
  });

  it("/enable shows what it reads now and turns it on only after a yes", async () => {
    const checkWatch = mock(async (_id: number) => CHECK);
    const enableWatch = mock(async (_id: number, _token: string) => ({
      ...KETTLE,
      status: "on" as const,
    }));
    await using s = await screen(
      fakeClient({ watches: async () => [KETTLE], checkWatch, enableWatch }),
      { height: 40 },
    );
    await enter(s, "/enable 3");
    const asked = await until(s, "Turn this watch on?");
    expect(asked).toContain("Yes, turn it on: it reads “$59.99” now"); // the dry run
    await press(s, "RETURN"); // keeping it off is the first choice
    const card = await gone(s, "Turn this watch on?");
    expect(card).toContain("Right now it reads: $59.99 (checked Fri 27 Nov 09:01)");
    expect(card).toContain("unchanged checks stay silent");
    expect(enableWatch).not.toHaveBeenCalled();

    await enter(s, "/enable");
    await until(s, "Turn on which watch?");
    await press(s, "RETURN");
    await until(s, "Turn this watch on?");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Watch #3 is on:");
    expect(enableWatch).toHaveBeenCalledWith(3, "abc123"); // the check the user saw
    expect(checkWatch).toHaveBeenCalledTimes(2);
  });

  it("/cancel stops a watch after asking", async () => {
    const cancelWatch = mock(async (_id: number) => {});
    await using s = await screen(fakeClient({ watches: async () => [KETTLE], cancelWatch }));
    await enter(s, "/cancel");
    await until(s, "Watch: the kettle's price");
    await press(s, "RETURN");
    await until(s, "Yes, cancel “watching the kettle's price”");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    await until(s, "Stopped watching “the kettle's price”.");
    expect(cancelWatch).toHaveBeenCalledWith(3);
  });

  it("a watch's report shows in the chat on screen with its own sign", async () => {
    const report: GatewayEvent = {
      kind: "watch",
      watch: 3,
      outcome: "reported",
      words: "the kettle's price: now $39.00. That was its last wake.",
      chat_id: "w3",
      conversation_id: "c1",
      stopped: true,
    };
    async function* events(signal?: AbortSignal): AsyncGenerator<GatewayEvent> {
      yield report;
      yield* quiet(signal);
    }
    await using s = await screen(fakeClient({ events }));
    await until(s, "👀 the kettle's price: now $39.00. That was its last wake.");
  });
});
