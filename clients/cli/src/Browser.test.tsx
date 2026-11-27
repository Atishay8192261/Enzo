import { describe, expect, it, mock } from "bun:test";
import {
  done,
  enter,
  type events,
  fakeClient,
  gone,
  press,
  screen,
  slowly,
  until,
} from "./testing.tsx";

/** A browser task as the gateway streams it (ADR 0027): the sign's site, steps, then a card. */
function browsing(...rest: Parameters<typeof events>) {
  return slowly(
    20,
    { type: "browser", site: "shop.example" },
    { type: "tool_started", id: "b1", tool: "browser_click", label: "Clicking “Add to cart”" },
    {
      type: "tool_finished",
      id: "b1",
      tool: "browser_click",
      label: "Clicking “Add to cart”",
      status: "ok",
      latency_ms: 40,
    },
    ...rest,
  );
}

describe("Enzo using the browser, in the terminal", () => {
  it("shows a plain sign while the task runs, its steps in words, and drops it at the end", async () => {
    const send = mock(() =>
      browsing(
        { type: "text", text: "Adding it now." },
        { type: "browser", site: null },
        done({ text: "Adding it now." }),
      ),
    );
    await using s = await screen(fakeClient({ send }));
    await enter(s, "put the kettle in my cart");
    const frame = await until(s, "Enzo is using your browser: shop.example");
    expect(frame).toContain("esc stops it");
    const after = await gone(s, "Enzo is using your browser");
    expect(after).toContain("Clicking “Add to cart”");
    expect(after).not.toContain("browser_click"); // never a tool name (§10.3)
  });

  it("keeps the sign while a step waits for a yes, and Esc closes the browser", async () => {
    const stop = mock(async () => true);
    const send = mock(() =>
      browsing(
        {
          type: "approval_needed",
          id: "a1",
          tool: "browser_click",
          arguments: { ref: "e8", element: "Place order" },
          summary: "Click “Place order” on shop.example: it buys or pays for something",
          detail: "This sends to shop.example/order:\n  Card number: card number ending 4242",
          privacy: [],
        },
        done({ stop: "approval_pending", text: "Waiting" }),
      ),
    );
    await using s = await screen(fakeClient({ send, stop }), { height: 44 });
    await enter(s, "buy the kettle");
    await until(s, "Approve this action?");
    const card = await until(s, "Waiting for your approval."); // the turn is paused, not running
    expect(card).toContain("Click “Place order” on shop.example: it buys or pays for something");
    expect(card).toContain("card number ending 4242");
    expect(card).toContain("Enzo is using your browser: shop.example");
    await press(s, "ESCAPE");
    await until(s, "Closed the browser");
    expect(stop).toHaveBeenCalledWith("c1");
    await gone(s, "Enzo is using your browser");
  });

  it("a step cut off by Esc says stopped, not failed", async () => {
    const send = mock(() =>
      slowly(
        20,
        { type: "browser", site: "shop.example" },
        {
          type: "tool_started",
          id: "b1",
          tool: "browser_open",
          label: "Opening shop.example/slow",
        },
        {
          type: "tool_finished",
          id: "b1",
          tool: "browser_open",
          label: "Opening shop.example/slow",
          status: "stopped",
          latency_ms: 900,
        },
        done({ stop: "cancelled", text: "Stopped at your request." }),
      ),
    );
    await using s = await screen(fakeClient({ send }));
    await enter(s, "open the slow page");
    const frame = await until(s, "· stopped");
    expect(frame).toContain("Opening shop.example/slow");
    expect(frame).not.toContain("· failed");
  });
});
