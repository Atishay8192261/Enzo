import { describe, expect, it, mock } from "bun:test";
import { mkdtempSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AccountsClient, ChatClient, ConversationMemory } from "@enzo/chat";
import type { AgentEvent, Brain, Conversation, ProviderConnection } from "@enzo/sdk";
import { testRender } from "@opentui/react/test-utils";
import type React from "react";
import { act } from "react";
import { App } from "./App.tsx";
import { fileMemory } from "./memory.ts";

const BRAINS: Brain[] = [
  { name: "claude-opus", provider: "anthropic", model: "claude-opus-5-5", priced: true },
  { name: "grok", provider: "xai", model: "grok-4.7", priced: false },
];
const CONVERSATION: Conversation = { id: "c1", kind: "main", title: null, brain: null };

async function* events(...list: AgentEvent[]): AsyncGenerator<AgentEvent> {
  for (const e of list) yield e;
}

function memory(start: string | null = null): ConversationMemory {
  let id = start;
  return {
    get: () => id,
    set: (v) => {
      id = v;
    },
  };
}

const CONNECTIONS: ProviderConnection[] = [
  {
    provider: "anthropic",
    connected: true,
    method: "api_key",
    connected_at: "2026-11-17T20:00:00Z",
    methods: ["api_key"],
    models: ["claude-opus-5-5"],
  },
  {
    provider: "xai",
    connected: false,
    method: null,
    connected_at: null,
    methods: ["api_key"],
    models: ["grok-4.7"],
  },
];

type Client = ChatClient & AccountsClient;

function fakeClient(overrides: Partial<Client> = {}): Client {
  return {
    connections: async () => CONNECTIONS,
    connect: mock(async () => {}),
    disconnect: mock(async () => {}),
    checkConnection: async () => ({ ok: true, detail: null }),
    health: async () => ({ status: "ok", version: "1", env: "test" }),
    brains: async () => BRAINS,
    createConversation: async () => CONVERSATION,
    conversation: async () => CONVERSATION,
    messages: async () => [],
    chooseBrain: mock(async () => {}),
    send: () => events(),
    resume: () => events(),
    decide: mock(async () => {}),
    ...overrides,
  };
}

async function screen(client: Client, onQuit = () => {}) {
  return mount(<App client={client} memory={memory()} onQuit={onQuit} />, 30, "Message Enzo");
}

/** Render inside act() (start-up work is async), then wait for the first expected screen. */
async function mount(node: React.ReactNode, height: number, ready: string): Promise<Screen> {
  let setup: Screen | undefined;
  await act(async () => {
    setup = await testRender(node, { width: 90, height });
  });
  if (!setup) throw new Error("render failed");
  const screen = setup;
  // Tear down inside act() too: unmounting the root updates React.
  screen[Symbol.asyncDispose] = () => act(() => screen.dispose());
  await until(screen, ready);
  return screen;
}

type Screen = Awaited<ReturnType<typeof testRender>>;

/** Type a line and press Enter, inside act() like any user-driven update. */
async function enter(s: Screen, text: string): Promise<void> {
  await act(async () => {
    await s.mockInput.typeText(text);
    s.mockInput.pressEnter();
  });
}

async function press(s: Screen, key: string): Promise<void> {
  await act(async () => {
    s.mockInput.pressKey(key);
  });
}

/** Wait for text on screen while letting real time pass, so async work (streams, network) runs. */
async function until(s: Screen, text: string, ms = 3000): Promise<string> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    // Inside act(): React applies the updates async work made meanwhile, as it does in real use.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    await s.flush();
    const frame = s.captureCharFrame();
    if (frame.includes(text)) return frame;
  }
  throw new Error(`never saw "${text}" on screen:\n${s.captureCharFrame()}`);
}

describe("terminal chat (OpenTUI)", () => {
  it("sends a message and streams the reply", async () => {
    const client = fakeClient({
      send: () =>
        events(
          { type: "text", text: "Paris" },
          { type: "done", stop: "final_answer", text: "Paris", steps: 1, cost_usd: 0.0021 },
        ),
    });
    await using s = await screen(client);
    await enter(s, "Capital of France?");
    const frame = await until(s, "enzo: Paris");
    expect(frame).toContain("you: Capital of France?");
    expect(frame).toContain("cost $0.0021");
  });

  it("keeps the first keystroke and clears the input after sending", async () => {
    const send = mock(() =>
      events({ type: "done", stop: "final_answer", text: "ok", steps: 1, cost_usd: null }),
    );
    await using s = await screen(fakeClient({ send }));
    await enter(s, "hello enzo");
    await until(s, "enzo: ok");
    expect(send).toHaveBeenCalledWith(
      "c1",
      "hello enzo",
      expect.objectContaining({ device: "cli" }),
    );
    await act(async () => {
      await s.mockInput.typeText("again");
    });
    const frame = await until(s, "again");
    expect(frame).not.toContain("hello enzoagain");
  });

  it("asks for approval of the exact action and resumes on y", async () => {
    const decide = mock(async () => {});
    const client = fakeClient({
      decide,
      send: () =>
        events(
          {
            type: "approval_needed",
            id: "appr_1",
            tool: "send_email",
            arguments: { to: "bob@example.com" },
          },
          { type: "done", stop: "approval_pending", text: "Waiting", steps: 1, cost_usd: null },
        ),
      resume: () =>
        events({ type: "done", stop: "final_answer", text: "Sent.", steps: 2, cost_usd: null }),
    });
    await using s = await screen(client);
    await enter(s, "email bob");
    const card = await until(s, "Approve this action? (y / n)");
    expect(card).toContain('"to": "bob@example.com"');
    await press(s, "y");
    await until(s, "enzo: Sent.");
    expect(decide).toHaveBeenCalledWith("c1", "appr_1", true);
  });

  it("switches brains with a command and rejects unknown ones", async () => {
    const chooseBrain = mock(async () => {});
    await using s = await screen(fakeClient({ chooseBrain }));
    await enter(s, "/brain ghost");
    await until(s, "Unknown brain: ghost");
    await enter(s, "/brain grok");
    await until(s, "Brain: grok");
    expect(chooseBrain).toHaveBeenCalledWith("c1", "grok");
  });

  it("quits on /quit", async () => {
    const onQuit = mock(() => {});
    await using s = await screen(fakeClient(), onQuit);
    await enter(s, "/quit");
    await until(s, "Message Enzo");
    expect(onQuit).toHaveBeenCalledTimes(1);
  });

  it("says plainly when the gateway is down", async () => {
    const client = fakeClient({ health: () => Promise.reject(new Error("ECONNREFUSED")) });
    await using _s = await mount(
      <App client={client} memory={memory()} onQuit={() => {}} />,
      10,
      "Gateway unreachable: ECONNREFUSED",
    );
  });
});

describe("provider accounts", () => {
  const KEY = "fake-xai-000000000000";

  it("logs in at a hidden prompt: the key is never drawn", async () => {
    const connect = mock(async () => {});
    await using s = await screen(fakeClient({ connect }));
    await enter(s, "/login xai");
    await until(s, "Paste your xai API key (hidden)");
    let frames = "";
    await act(async () => {
      await s.mockInput.typeText(KEY);
    });
    frames += await until(s, `${KEY.length} characters`);
    await act(async () => {
      s.mockInput.pressEnter();
    });
    frames += await until(s, "Connected xai.");
    expect(connect).toHaveBeenCalledWith("xai", KEY);
    expect(frames).not.toContain(KEY);
    expect(frames).not.toContain("fake-xai");
    await until(s, "Message Enzo"); // back to chat
  });

  it("takes a bracketed paste and trims the newline", async () => {
    const connect = mock(async () => {});
    await using s = await screen(fakeClient({ connect }));
    await enter(s, "/login xai");
    await until(s, "(hidden)");
    await act(async () => {
      await s.mockInput.pasteBracketedText(`${KEY}\n`);
    });
    const frame = await until(s, `${KEY.length} characters`);
    expect(frame).not.toContain(KEY);
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, "Connected xai.");
    expect(connect).toHaveBeenCalledWith("xai", KEY);
  });

  it("Esc cancels without saving", async () => {
    const connect = mock(async () => {});
    await using s = await screen(fakeClient({ connect }));
    await enter(s, "/login xai");
    await until(s, "(hidden)");
    await act(async () => {
      await s.mockInput.typeText("abc");
    });
    await until(s, "3 characters");
    await press(s, "ESCAPE");
    await until(s, "Login cancelled. Nothing was saved.");
    expect(connect).not.toHaveBeenCalled();
  });

  it("shows the provider's refusal and never the key", async () => {
    const connect = mock(() =>
      Promise.reject(new Error("/connections/xai failed: HTTP 422: xai rejected this key")),
    );
    await using s = await screen(fakeClient({ connect }));
    await enter(s, "/login xai");
    await until(s, "(hidden)");
    await act(async () => {
      await s.mockInput.typeText(KEY);
      s.mockInput.pressEnter();
    });
    const frame = await until(s, "xai rejected this key");
    expect(frame).not.toContain(KEY);
  });

  it("refuses keys typed on the command line and unknown providers", async () => {
    const connect = mock(async () => {});
    await using s = await screen(fakeClient({ connect }));
    await enter(s, `/login xai ${KEY}`);
    const frame = await until(s, "Never type a key on the command line");
    expect(frame).not.toContain(KEY);
    await enter(s, "/login ghost");
    await until(s, "Unknown provider: ghost. Providers: anthropic, xai");
    expect(connect).not.toHaveBeenCalled();
  });

  it("lists, checks and disconnects", async () => {
    const disconnect = mock(async () => {});
    const checkConnection = mock(async () => ({ ok: false, detail: "xai rejected this key" }));
    await using s = await screen(fakeClient({ disconnect, checkConnection }));
    await enter(s, "/connections");
    await until(s, "anthropic: connected (API key, since 2026-11-17) · claude-opus-5-5");
    await until(s, "xai: not connected: /login xai · grok-4.7");
    await enter(s, "/check xai");
    await until(s, "xai: xai rejected this key");
    await enter(s, "/logout xai");
    await until(s, "Disconnected xai. Its key was deleted.");
    expect(disconnect).toHaveBeenCalledWith("xai");
    await enter(s, "/logout");
    await until(s, "Usage: /logout <provider>");
  });
});

describe("fileMemory", () => {
  it("remembers the conversation in a private file", () => {
    const dir = mkdtempSync(join(tmpdir(), "enzo-"));
    const m = fileMemory({ ENZO_CONFIG_DIR: dir });
    expect(m.get()).toBeNull();
    m.set("c-42");
    expect(fileMemory({ ENZO_CONFIG_DIR: dir }).get()).toBe("c-42");
    expect(statSync(join(dir, "conversation")).mode & 0o777).toBe(0o600);
  });
});
