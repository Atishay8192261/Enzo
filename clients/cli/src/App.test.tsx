import { mkdtempSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ChatClient, ConversationMemory } from "@enzo/chat";
import type { AgentEvent, Brain, Conversation } from "@enzo/sdk";
import { render } from "ink-testing-library";
import { describe, expect, it, vi } from "vitest";
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

function fakeClient(overrides: Partial<ChatClient> = {}): ChatClient {
  return {
    health: async () => ({ status: "ok", version: "1", env: "test" }),
    brains: async () => BRAINS,
    createConversation: async () => CONVERSATION,
    conversation: async () => CONVERSATION,
    messages: async () => [],
    chooseBrain: vi.fn(async () => {}),
    send: () => events(),
    resume: () => events(),
    decide: vi.fn(async () => {}),
    ...overrides,
  };
}

async function until(frame: () => string | undefined, text: string | RegExp): Promise<string> {
  for (let i = 0; i < 100; i++) {
    const f = frame() ?? "";
    if (typeof text === "string" ? f.includes(text) : text.test(f)) return f;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error(`never saw ${String(text)} in:\n${frame()}`);
}

async function type(stdin: { write: (s: string) => void }, text: string): Promise<void> {
  for (const ch of text) {
    stdin.write(ch);
    await new Promise((r) => setTimeout(r, 2));
  }
  stdin.write("\r");
}

describe("terminal chat", () => {
  it("sends a message and streams the reply", async () => {
    const client = fakeClient({
      send: () =>
        events(
          { type: "text", text: "Paris" },
          { type: "done", stop: "final_answer", text: "Paris", steps: 1, cost_usd: 0.0021 },
        ),
    });
    const { lastFrame, stdin } = render(<App client={client} memory={memory()} />);
    await until(lastFrame, "Message Enzo");
    await type(stdin, "Capital of France?");
    const frame = await until(lastFrame, "enzo: Paris");
    expect(frame).toContain("you: Capital of France?");
    expect(frame).toContain("cost $0.0021");
  });

  it("asks for approval of the exact action and resumes on y", async () => {
    const decide = vi.fn(async () => {});
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
    const { lastFrame, stdin } = render(<App client={client} memory={memory()} />);
    await until(lastFrame, "Message Enzo");
    await type(stdin, "email bob");
    const card = await until(lastFrame, "Approve this action? (y / n)");
    expect(card).toContain('"to": "bob@example.com"');
    stdin.write("y");
    await until(lastFrame, "enzo: Sent.");
    expect(decide).toHaveBeenCalledWith("c1", "appr_1", true);
  });

  it("switches brains with a command and rejects unknown ones", async () => {
    const chooseBrain = vi.fn(async () => {});
    const { lastFrame, stdin } = render(
      <App client={fakeClient({ chooseBrain })} memory={memory()} />,
    );
    await until(lastFrame, "Message Enzo");
    await type(stdin, "/brain ghost");
    await until(lastFrame, "Unknown brain: ghost");
    await type(stdin, "/brain grok");
    await until(lastFrame, "Brain: grok");
    expect(chooseBrain).toHaveBeenCalledWith("c1", "grok");
  });

  it("handles a whole line arriving at once (fast typing, pastes, remote terminals)", async () => {
    const send = vi.fn(() =>
      events({ type: "done", stop: "final_answer", text: "ok", steps: 1, cost_usd: null }),
    );
    const { lastFrame, stdin } = render(<App client={fakeClient({ send })} memory={memory()} />);
    await until(lastFrame, "Message Enzo");
    stdin.write("hello enzo\r");
    await until(lastFrame, "enzo: ok");
    expect(send).toHaveBeenCalledWith(
      "c1",
      "hello enzo",
      expect.objectContaining({ device: "cli" }),
    );
    stdin.write("second\rthird\r");
    await until(lastFrame, "you: second");
  });

  it("says plainly when the gateway is down", async () => {
    const client = fakeClient({ health: () => Promise.reject(new Error("ECONNREFUSED")) });
    const { lastFrame } = render(<App client={client} memory={memory()} />);
    await until(lastFrame, "Gateway unreachable: ECONNREFUSED");
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
