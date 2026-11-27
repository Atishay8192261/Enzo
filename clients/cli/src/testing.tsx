/** Helpers for the terminal app's tests: a fake gateway, and driving the screen like a user. */
import { mock } from "bun:test";
import type {
  AccountsClient,
  ChatClient,
  ChatsClient,
  ConversationMemory,
  GoalsClient,
  HelpersClient,
  MemoryClient,
  PrivacyClient,
  ScheduleClient,
  Start,
} from "@enzo/chat";
import type {
  AgentEvent,
  Brain,
  Conversation,
  ConversationUsage,
  GatewayEvent,
  Helper,
  ProviderConnection,
  Usage,
} from "@enzo/sdk";
import type { TerminalCapabilities } from "@opentui/core";
import { testRender } from "@opentui/react/test-utils";
import type React from "react";
import { act } from "react";
import { App } from "./App.tsx";

export const BRAINS: Brain[] = [
  {
    name: "claude-sonnet",
    provider: "anthropic",
    model: "claude-sonnet-5-5",
    priced: true,
    tier: "balanced",
    recommended: true,
    note: null,
    available: true,
  },
  {
    name: "grok",
    provider: "xai",
    model: "grok-4.7",
    priced: false,
    tier: "powerful",
    recommended: true,
    note: null,
    available: false,
  },
];

export const CONVERSATION: Conversation = { id: "c1", kind: "main", title: null, brain: null };

export const CONNECTIONS: ProviderConnection[] = [
  {
    provider: "anthropic",
    connected: true,
    method: "api_key",
    connected_at: "2026-11-17T20:00:00Z",
    methods: ["api_key"],
    models: ["claude-opus-5-5"],
    tools: [],
    purpose: null,
    label: null,
  },
  {
    provider: "xai",
    connected: false,
    method: null,
    connected_at: null,
    methods: ["api_key"],
    models: ["grok-4.7"],
    tools: [],
    purpose: null,
    label: null,
  },
];

export const NONE: Usage = {
  input_tokens: 0,
  output_tokens: 0,
  cached_input_tokens: 0,
  calls: 0,
  cost_usd: 0,
  plan: false,
  models: [],
  model_ms: 0,
  started_at: null,
};

export const USED: Usage = {
  ...NONE,
  input_tokens: 1388,
  output_tokens: 81,
  calls: 1,
  cost_usd: 0.0021,
  models: ["claude-sonnet-5-5"],
  model_ms: 3100,
};

type Done = Extract<AgentEvent, { type: "done" }>;

/** A turn's last event: a final answer unless told otherwise. */
export function done(over: Partial<Done> = {}): Done {
  return {
    type: "done",
    stop: "final_answer",
    text: "ok",
    steps: 1,
    cost_usd: null,
    brain: null,
    model: null,
    failure: null,
    plan: false,
    usage: NONE,
    ...over,
  };
}

export async function* events(...list: AgentEvent[]): AsyncGenerator<AgentEvent> {
  for (const e of list) yield e;
}

/** Events with a pause before each, so a screen can be seen mid-reply. */
export async function* slowly(ms: number, ...list: AgentEvent[]): AsyncGenerator<AgentEvent> {
  for (const e of list) {
    await new Promise((r) => setTimeout(r, ms));
    yield e;
  }
}

export function memory(start: string | null = null): ConversationMemory {
  let id = start;
  return {
    get: () => id,
    set: (v) => {
      id = v;
    },
  };
}

export type Client = ChatClient &
  AccountsClient &
  ChatsClient &
  MemoryClient &
  PrivacyClient &
  ScheduleClient &
  GoalsClient &
  HelpersClient;

/** An event stream with nothing to say: it stays open until the app closes it. */
export async function* quiet(signal?: AbortSignal): AsyncGenerator<GatewayEvent> {
  await new Promise<void>((resolve) => {
    if (signal?.aborted) return resolve();
    signal?.addEventListener("abort", () => resolve(), { once: true });
  });
}

/** A helper as the gateway lists it. */
export function helperOf(id: number, more: Partial<Helper> = {}): Helper {
  return {
    id,
    title: "Compare 5 laptops",
    status: "working",
    depth: 1,
    chat_id: `h${id}`,
    parent_id: "c1",
    conversation_id: "c1",
    coordinator_id: null,
    created_at: "2026-11-27T10:00:00Z",
    finished_at: null,
    steps: 4,
    calls: 5,
    cost_usd: 0.02,
    plan: false,
    waiting: null,
    ...more,
  };
}

export function fakeClient(overrides: Partial<Client> = {}): Client {
  const usage: ConversationUsage = { total: NONE, turns: [] };
  return {
    connections: async () => CONNECTIONS,
    connect: mock(async () => {}),
    disconnect: mock(async () => ({ remote_sign_out: null })),
    checkConnection: async () => ({ ok: true, detail: null }),
    startChatGPTSignIn: mock(async () => ({
      login_id: "l1",
      url: "http://127.0.0.1:8787/auth/chatgpt/l1",
    })),
    chatGPTSignIn: async () => ({
      status: "connected" as const,
      message: "Signed in with ChatGPT as ada@example.com.",
      first_time: true,
      usage_url: "https://chatgpt.com/settings/usage",
    }),
    startGoogleSignIn: mock(async () => ({
      login_id: "g1",
      url: "http://127.0.0.1:8787/auth/google/g1",
      needs_client: false,
      message: null,
    })),
    googleSignIn: async () => ({
      status: "connected" as const,
      message: "Connected Google as ada@example.com: Gmail (read, send, drafts).",
    }),
    setGoogleClient: mock(async () => {}),
    health: async () => ({ status: "ok", version: "1", env: "test" }),
    brains: async () => BRAINS,
    createConversation: async () => CONVERSATION,
    conversation: async () => CONVERSATION,
    messages: async () => [],
    usage: async () => usage,
    conversations: async () => [],
    renameConversation: mock(async () => {}),
    deleteConversation: mock(async () => {}),
    memories: async () => ({ memories: [], file: "~/Enzo/Memory.md" }),
    goals: async () => ({ goals: [], file: "~/Enzo/Goals.md" }),
    scheduled: async () => [],
    cancelScheduled: mock(async () => {}),
    conversationScheduled: async () => [],
    helpers: async () => [],
    stopHelper: mock(async (id: number) => ({ stopped: true, helper: helperOf(id) })),
    conversationHelpers: async () => [],
    events: quiet,
    forgetMemory: mock(async () => {}),
    privacyRules: async () => [],
    removePrivacyRule: mock(async () => {}),
    chooseBrain: mock(async () => {}),
    send: () => events(),
    resume: () => events(),
    decide: mock(async () => {}),
    stop: mock(async () => false),
    ...overrides,
  };
}

export type Screen = Awaited<ReturnType<typeof testRender>>;

export interface ScreenOptions {
  onQuit?: () => void;
  openUrl?: (url: string) => Promise<void>;
  copy?: (text: string) => Promise<boolean>;
  memory?: ConversationMemory;
  width?: number;
  height?: number;
  /** Text to wait for before the test starts. */
  ready?: string;
  healthEveryMs?: number;
  /** A terminal that reports keys with the kitty protocol (Shift+Enter told apart from Enter). */
  kitty?: boolean;
  /** A fresh chat (`enzo`, the default) or the last one (`enzo --continue`). */
  start?: Start;
  /** What the terminal says it supports once it answers (e.g. the kitty keyboard protocol). */
  terminal?: Partial<TerminalCapabilities>;
}

/** The app on a test screen, ready to type into. */
export async function screen(client: Client, options: ScreenOptions = {}): Promise<Screen> {
  const s = await mount(
    <App
      client={client}
      memory={options.memory ?? memory()}
      onQuit={options.onQuit ?? (() => {})}
      openUrl={options.openUrl ?? (async () => {})}
      copy={options.copy ?? (async () => true)}
      version="0.1.0"
      healthEveryMs={options.healthEveryMs ?? 60_000}
      start={options.start ?? "fresh"}
    />,
    {
      width: options.width ?? 90,
      height: options.height ?? 34,
      ...(options.kitty ? { kittyKeyboard: true } : {}),
    },
    options.ready ?? "Message Enzo",
  );
  if (options.terminal) answer(s, options.terminal);
  return s;
}

/** The terminal answers the capability queries, as a real one does shortly after start. */
export function answer(s: Screen, capabilities: Partial<TerminalCapabilities>): void {
  const renderer = s.renderer as unknown as { _capabilities: TerminalCapabilities | null };
  renderer._capabilities = {
    ...(renderer._capabilities ?? {}),
    ...capabilities,
  } as TerminalCapabilities;
  s.renderer.emit("capabilities", renderer._capabilities);
}

/** Render inside act() (start-up work is async), then wait for the first expected screen. */
export async function mount(
  node: React.ReactNode,
  size: { width: number; height: number; kittyKeyboard?: boolean },
  ready: string,
): Promise<Screen> {
  let setup: Screen | undefined;
  await act(async () => {
    // Like the real app (bin.tsx): Ctrl+C is the app's to handle, not the renderer's.
    setup = await testRender(node, { ...size, exitOnCtrlC: false });
  });
  if (!setup) throw new Error("render failed");
  const s = setup;
  // Tear down inside act() too: unmounting the root updates React.
  s[Symbol.asyncDispose] = () => act(() => s.dispose());
  await until(s, ready);
  return s;
}

/** Type a line and press Enter, inside act() like any user-driven update. */
export async function enter(s: Screen, text: string): Promise<void> {
  await type(s, text);
  await act(async () => {
    s.mockInput.pressEnter();
  });
}

export async function type(s: Screen, text: string): Promise<void> {
  await act(async () => {
    await s.mockInput.typeText(text);
  });
  await settle(s);
}

export async function press(
  s: Screen,
  key: string,
  modifiers?: { ctrl?: boolean; shift?: boolean; meta?: boolean },
): Promise<void> {
  await act(async () => {
    s.mockInput.pressKey(key, modifiers);
  });
  await settle(s);
}

export async function arrow(s: Screen, direction: "up" | "down" | "left" | "right") {
  await act(async () => {
    s.mockInput.pressArrow(direction);
  });
  await settle(s);
}

/** A mocked send that records its calls, typed like the real one. */
export function sender(make: () => AsyncGenerator<AgentEvent> = () => events(done())) {
  return mock((_id: string, _text: string, _context?: unknown) => make());
}

export async function paste(s: Screen, text: string): Promise<void> {
  await act(async () => {
    await s.mockInput.pasteBracketedText(text);
  });
  await settle(s);
}

/** Let React and the renderer catch up with real time (OpenTUI's own wait does not). */
export async function settle(s: Screen, ms = 30): Promise<string> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
  await s.flush();
  return s.captureCharFrame();
}

/** Wait for text on screen while letting real time pass, so async work (streams, network) runs. */
export async function until(s: Screen, text: string | RegExp, ms = 3000): Promise<string> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const frame = await settle(s, 10);
    if (typeof text === "string" ? frame.includes(text) : text.test(frame)) return frame;
  }
  throw new Error(`never saw ${String(text)} on screen:\n${s.captureCharFrame()}`);
}

/** Wait until text is gone from the screen. */
export async function gone(s: Screen, text: string, ms = 3000): Promise<string> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const frame = await settle(s, 10);
    if (!frame.includes(text)) return frame;
  }
  throw new Error(`"${text}" stayed on screen:\n${s.captureCharFrame()}`);
}

/** The screen's lines, trimmed on the right. */
export const lines = (frame: string) => frame.split("\n").map((l) => l.trimEnd());
