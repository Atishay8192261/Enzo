import { describe, expect, it, mock } from "bun:test";
import { GatewayUnreachableError } from "@enzo/sdk";
import {
  BRAINS,
  done,
  enter,
  events,
  fakeClient,
  gone,
  lines,
  screen,
  sender,
  settle,
  USED,
  until,
} from "./testing.tsx";

const WORDMARK = "███████╗ ███╗   ██╗";

describe("the opening screen (#35)", () => {
  it("shows the ENZO wordmark, the version, the account and keys, with the input under it", async () => {
    await using s = await screen(fakeClient(), { ready: WORDMARK });
    const frame = s.captureCharFrame();
    expect(frame).toContain("your personal agent · 0.1.0");
    expect(frame).toContain("auto · anthropic · API key");
    expect(frame).toContain("/ commands · ctrl+p palette · /new new chat · /help · ctrl+d quit");
    const rows = lines(frame);
    const logo = rows.findIndex((l) => l.includes(WORDMARK));
    const input = rows.findIndex((l) => l.includes("❯ Message Enzo"));
    expect(input).toBeGreaterThan(logo); // the input sits right under the wordmark
    expect(frame).not.toContain("No model account is connected yet");
  });

  it("falls back to a plain title in a narrow or short terminal", async () => {
    await using narrow = await screen(fakeClient(), { width: 58, ready: "Message Enzo" });
    expect(narrow.captureCharFrame()).not.toContain(WORDMARK);
    expect(narrow.captureCharFrame()).toContain("Enzo");
    await using short = await screen(fakeClient(), { height: 16, ready: "Message Enzo" });
    expect(short.captureCharFrame()).not.toContain(WORDMARK);
  });

  it("points a new user to /login chatgpt, on lines that are never split", async () => {
    const offline = BRAINS.map((b) => ({ ...b, available: false }));
    const client = fakeClient({
      brains: async () => offline,
      connections: async () => [],
    });
    await using s = await screen(client, { width: 60, ready: "No model account is connected yet" });
    const rows = lines(s.captureCharFrame());
    expect(rows.some((l) => l.includes("your ChatGPT plan, no key needed: /login chatgpt"))).toBe(
      true,
    );
  });

  it("gives way to the chat once the first message is sent", async () => {
    await using s = await screen(fakeClient({ send: () => events(done({ text: "Hi!" })) }), {
      ready: WORDMARK,
    });
    await enter(s, "hello");
    const frame = await until(s, "Hi!");
    expect(frame).not.toContain(WORDMARK);
    expect(frame).toContain("◆ ENZO  │  hello"); // the chat's title is its first message
  });
});

describe("the layout (#35)", () => {
  it("a wide terminal shows the side panel: what this chat used, the model and accounts", async () => {
    await using s = await screen(
      fakeClient({ send: () => events(done({ text: "ok", usage: USED })) }),
      {
        width: 130,
      },
    );
    expect(s.captureCharFrame()).not.toContain("This chat used"); // not on the opening screen
    await enter(s, "hello");
    const frame = await until(s, "This chat used");
    expect(frame).toContain("tokens in");
    expect(frame).toContain("1,388");
    expect(frame).toContain("● anthropic · API key");
    expect(frame).toContain("○ xai");
    await enter(s, "/sidebar");
    await gone(s, "This chat used");
    await enter(s, "/sidebar");
    await until(s, "This chat used");
  });

  it("a narrow terminal hides it until asked", async () => {
    await using s = await screen(fakeClient({ send: () => events(done()) }), { width: 90 });
    await enter(s, "hello");
    await until(s, "◆ enzo");
    expect(s.captureCharFrame()).not.toContain("This chat used");
    await enter(s, "/sidebar");
    await until(s, "This chat used");
  });

  it("the status bar shows the model, the account and what the chat used", async () => {
    const chooseBrain = mock(async (_id: string, _choice: string | null) => {});
    await using s = await screen(
      fakeClient({ chooseBrain, send: () => events(done({ text: "ok", usage: USED })) }),
      { width: 120 },
    );
    await enter(s, "hello");
    const frame = await until(s, "this chat: 1,469 tokens · $0.0021");
    const bar = lines(frame).filter(Boolean).at(-1) ?? "";
    expect(bar).toContain("auto · anthropic · API key");
    expect(bar).toContain("ctrl+p palette");
    await enter(s, "/model claude-sonnet");
    await until(s, /claude-sonnet · anthropic · API key · this chat/);
  });

  it("in a narrow terminal the status bar's two sides never run into each other", async () => {
    await using s = await screen(
      fakeClient({ send: () => events(done({ text: "ok", usage: USED })) }),
      { width: 60 },
    );
    await enter(s, "hello");
    const frame = await until(s, "this chat");
    const bar = lines(frame).filter(Boolean).at(-1) ?? "";
    expect(bar.length).toBeLessThanOrEqual(60);
    expect(bar).not.toContain("ctrl+p"); // the hints gave way first
    expect(bar.trim()).toMatch(/^auto · anthropic · API key · this chat/);
  });

  it("in a very narrow terminal the status bar is cut with an ellipsis, not overlapped", async () => {
    await using s = await screen(
      fakeClient({ send: () => events(done({ text: "ok", usage: USED })) }),
      { width: 40 },
    );
    await enter(s, "hello");
    await until(s, "└");
    const frame = await until(s, "…");
    const bar = lines(frame).filter(Boolean).at(-1) ?? "";
    expect(bar.length).toBeLessThanOrEqual(40);
    expect(bar.trim()).toMatch(/^auto · anthropic · .*…$/);
    // the line under the input gives up its hint instead of running into it
    expect(frame).not.toContain("enter send");
    expect(lines(frame).some((l) => l.trim().startsWith("auto · anthropic"))).toBe(true);
  });
});

describe("when Enzo can't be reached (#44)", () => {
  it("says so plainly, keeps the message, retries by itself and offers to send it again", async () => {
    let up = true;
    const health = mock(async () => {
      if (!up) throw new GatewayUnreachableError("http://127.0.0.1:8787");
      return { status: "ok" as const, version: "1", env: "test" };
    });
    const send = sender(() =>
      up
        ? events(done({ text: "Yes, still here." }))
        : (async function* () {
            yield* [];
            throw new GatewayUnreachableError("http://127.0.0.1:8787");
          })(),
    );
    await using s = await screen(fakeClient({ health, send }), { healthEveryMs: 50 });
    up = false;
    await enter(s, "are you still there?");
    const down = await until(s, "Not sent: Enzo can't be reached right now.");
    expect(down).toContain("you · not sent");
    expect(down).toContain("Can't reach Enzo at 127.0.0.1:8787. Retrying every few seconds…");
    expect(down).toContain("If it stays down, quit (ctrl+d) and run enzo again.");
    expect(down).toContain("○ offline · retrying");
    expect(down).not.toMatch(/ECONNREFUSED|Unable to connect|fetch failed/);
    up = true; // the gateway is back
    const back = await until(s, "Connected to Enzo again.");
    expect(back).toContain("/retry sends your message again.");
    expect(back).toContain("● connected");
    await gone(s, "Can't reach Enzo at");
    await enter(s, "/retry");
    await until(s, "Yes, still here.");
    expect(send.mock.calls.map((c) => c[1])).toEqual([
      "are you still there?",
      "are you still there?",
    ]);
  });

  it("notices on its own when the gateway stops, before anything is sent", async () => {
    let up = true;
    const health = async () => {
      if (!up) throw new GatewayUnreachableError("http://127.0.0.1:8787");
      return { status: "ok" as const, version: "1", env: "test" };
    };
    await using s = await screen(fakeClient({ health, send: () => events(done()) }), {
      healthEveryMs: 50,
    });
    await enter(s, "hello");
    await until(s, "● connected");
    up = false;
    await until(s, "○ offline · retrying");
    up = true;
    await until(s, "● connected");
    expect(s.captureCharFrame()).toContain("Connected to Enzo again.");
  });

  it("starting while Enzo is down shows the opening screen, then connects when it is up", async () => {
    let up = false;
    const health = async () => {
      if (!up) throw new GatewayUnreachableError("http://127.0.0.1:8787");
      return { status: "ok" as const, version: "1", env: "test" };
    };
    await using s = await screen(fakeClient({ health }), {
      healthEveryMs: 50,
      ready: "Can't reach Enzo at 127.0.0.1:8787",
    });
    expect(s.captureCharFrame()).toContain("your personal agent");
    up = true;
    await gone(s, "Can't reach Enzo at");
    const frame = await settle(s, 100);
    expect(frame).toContain("auto · anthropic · API key");
  });
});
