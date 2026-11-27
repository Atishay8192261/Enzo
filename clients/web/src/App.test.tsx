import type { AccountsClient, ChatClient, ImagesClient } from "@enzo/chat";
import { NO_USAGE } from "@enzo/chat";
import {
  type AgentEvent,
  type Brain,
  type Conversation,
  GatewayError,
  type StoredMessage,
} from "@enzo/sdk";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, CANT_SEE } from "./App.tsx";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

const BRAINS: Brain[] = [
  {
    name: "claude-opus",
    provider: "anthropic",
    model: "claude-opus-5-5",
    priced: true,
    tier: "powerful",
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
const CONVERSATION: Conversation = { id: "c1", kind: "main", title: null, brain: null };

async function* events(...list: AgentEvent[]): AsyncGenerator<AgentEvent> {
  for (const e of list) yield e;
}

type Client = ChatClient & AccountsClient & ImagesClient;

function fakeClient(overrides: Partial<Client> = {}): Client {
  return {
    uploadImage: vi.fn(async (_data: unknown, options?: { name?: string }) => ({
      id: "img_0123456789abcdef",
      media_type: "image/png",
      width: 1456,
      height: 819,
      bytes: 1000,
      name: options?.name ?? null,
      stripped: [],
    })),
    connections: async () => [],
    connect: vi.fn(async () => {}),
    disconnect: vi.fn(async () => ({ remote_sign_out: null })),
    checkConnection: async () => ({ ok: true, detail: null }),
    startChatGPTSignIn: async () => ({
      login_id: "l1",
      url: "http://127.0.0.1:8787/auth/chatgpt/l1",
    }),
    chatGPTSignIn: async () => ({
      status: "pending" as const,
      message: null,
      first_time: false,
      usage_url: "https://chatgpt.com/settings/usage",
    }),
    startGoogleSignIn: async () => ({
      login_id: "g1",
      url: "http://127.0.0.1:8787/auth/google/g1",
      needs_client: false,
      message: null,
    }),
    googleSignIn: async () => ({ status: "pending" as const, message: null }),
    setGoogleClient: vi.fn(async () => {}),
    health: async () => ({ status: "ok", version: "1", env: "test" }),
    brains: async () => BRAINS,
    createConversation: async () => CONVERSATION,
    conversation: async () => CONVERSATION,
    messages: async (): Promise<StoredMessage[]> => [],
    chooseBrain: vi.fn(async () => {}),
    send: () => events(),
    resume: () => events(),
    decide: vi.fn(async () => {}),
    stop: vi.fn(async () => false),
    usage: async () => ({ total: NO_USAGE, turns: [] }),
    conversationScheduled: async () => [],
    ...overrides,
  };
}

describe("App", () => {
  it("creates exactly one conversation even when React runs effects twice (StrictMode)", async () => {
    const createConversation = vi.fn(async () => CONVERSATION);
    render(
      <StrictMode>
        <App client={fakeClient({ createConversation })} />
      </StrictMode>,
    );
    await screen.findByRole("option", { name: /claude-opus/ });
    expect(createConversation).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("enzo.conversation")).toBe("c1");
  });

  it("connects, lists brains and streams a reply", async () => {
    const client = fakeClient({
      send: () =>
        events(
          { type: "text", text: "Paris" },
          {
            type: "done",
            stop: "final_answer",
            text: "Paris",
            steps: 1,
            cost_usd: 0.0021,
            brain: "claude-opus",
            model: "claude-opus-5-5",
            failure: null,
            plan: false,
            usage: NO_USAGE,
          },
        ),
    });
    render(<App client={client} />);
    const input = await screen.findByLabelText("Message");
    // grouped by tier; a model without a connected account is listed but cannot be picked
    const grok = screen.getByRole<HTMLOptionElement>("option", {
      name: "★ grok · grok-4.7 · cost unknown · connect xai",
    });
    expect(grok.disabled).toBe(true);
    expect(grok.parentElement?.getAttribute("label")).toBe("powerful: hardest tasks");
    expect(screen.getByRole<HTMLOptionElement>("option", { name: "Any fast model" }).disabled).toBe(
      true,
    );
    fireEvent.change(input, { target: { value: "Capital of France?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Paris")).toBeDefined();
    expect(screen.getByText("Capital of France?")).toBeDefined();
    expect(screen.getByText("cost $0.0021")).toBeDefined();
    expect(screen.getByText("claude-opus")).toBeDefined(); // which model wrote the reply
  });

  it("shows the exact action for approval and resumes after a yes", async () => {
    const decide = vi.fn(async () => {});
    const client = fakeClient({
      decide,
      send: () =>
        events(
          {
            type: "approval_needed",
            id: "appr_1",
            tool: "send_email",
            summary: "Email bob@example.com",
            detail: "To: bob@example.com\nhi",
            arguments: { to: "bob@example.com", body: "hi" },
          },
          {
            type: "done",
            stop: "approval_pending",
            text: "Waiting",
            steps: 1,
            cost_usd: null,
            brain: null,
            model: null,
            failure: null,
            plan: false,
            usage: NO_USAGE,
          },
        ),
      resume: () =>
        events({
          type: "done",
          stop: "final_answer",
          text: "Sent.",
          steps: 2,
          cost_usd: null,
          brain: null,
          model: null,
          failure: null,
          plan: false,
          usage: NO_USAGE,
        }),
    });
    render(<App client={client} />);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "email bob" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const card = await screen.findByRole("region", { name: "Approval needed" });
    expect(card.textContent).toContain("Email bob@example.com"); // plain words (§10.3)
    expect(card.textContent).not.toContain("send_email");
    expect(card.textContent).toContain("What it changes"); // the detail, e.g. a merge's diff
    expect(card.textContent).toContain("To: bob@example.com");
    expect(card.textContent).toContain('"to": "bob@example.com"'); // the exact action (§12.3)
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Sent.")).toBeDefined();
    expect(decide).toHaveBeenCalledWith("c1", "appr_1", true);
    expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
  });

  it("says when Enzo is using the browser, with a way to stop it (ADR 0027)", async () => {
    const stop = vi.fn(async () => true);
    const waiting: AgentEvent = {
      type: "done",
      stop: "approval_pending",
      text: "Waiting",
      steps: 1,
      cost_usd: null,
      brain: null,
      model: null,
      failure: null,
      plan: false,
      usage: NO_USAGE,
    };
    const client = fakeClient({
      stop,
      send: () => events({ type: "browser", site: "shop.example" }, waiting),
    });
    render(<App client={client} />);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "buy it" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const sign = await screen.findByRole("status");
    expect(sign.textContent).toContain("Enzo is using your browser: shop.example");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(screen.queryByText(/Enzo is using your browser/)).toBeNull());
    expect(stop).toHaveBeenCalledWith("c1");
  });

  it("restores the conversation history", async () => {
    const client = fakeClient({
      messages: async () => [
        {
          role: "user",
          parts: [
            { type: "text", text: "earlier question" },
            { type: "text", text: "[ctx]" },
          ],
        },
        { role: "assistant", parts: [{ type: "text", text: "earlier answer" }] },
      ],
    });
    render(<App client={client} />);
    expect(await screen.findByText("earlier answer")).toBeDefined();
    expect(screen.queryByText("[ctx]")).toBeNull();
  });

  it("stores the model or tier the user picks", async () => {
    const chooseBrain = vi.fn(async () => {});
    render(<App client={fakeClient({ chooseBrain })} />);
    await screen.findByLabelText("Message");
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "claude-opus" } });
    await waitFor(() => expect(chooseBrain).toHaveBeenCalledWith("c1", "claude-opus"));
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "powerful" } });
    await waitFor(() => expect(chooseBrain).toHaveBeenCalledWith("c1", "powerful"));
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "" } });
    await waitFor(() => expect(chooseBrain).toHaveBeenLastCalledWith("c1", null));
  });

  it("reloads the models after an account is connected", async () => {
    const brains = vi.fn(async () => BRAINS);
    const connections = async () => [
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
    render(<App client={fakeClient({ brains, connections })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts" }));
    fireEvent.change(await screen.findByLabelText("xai API key"), {
      target: { value: "fake-xai-000000000000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await screen.findByText("Connected xai.");
    await waitFor(() => expect(brains).toHaveBeenCalledTimes(2)); // startup + after connecting
  });

  it("tells web users to open Accounts, not to type /login (#25)", async () => {
    const client = fakeClient({
      send: () =>
        events({
          type: "done",
          stop: "brain_failure",
          text: "I could not get an answer: no model account is connected. Connect one: /login anthropic",
          steps: 0,
          cost_usd: 0,
          brain: null,
          model: null,
          failure: "not_connected",
          plan: false,
          usage: NO_USAGE,
        }),
    });
    render(<App client={client} />);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/No model account is connected yet/);
    expect(screen.queryByText(/\/login/)).toBeNull();
    expect(screen.getByText("no charge")).toBeDefined();
    expect(screen.queryByRole("dialog", { name: "Accounts" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Accounts" }));
    expect(screen.getByRole("dialog", { name: "Accounts" })).toBeDefined();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Accounts" })).toBeNull();
  });

  it("sends with Enter, keeps Shift+Enter for a new line", async () => {
    const send = vi.fn((_id: string, _text: string) => events());
    render(<App client={fakeClient({ send })} />);
    const box = await screen.findByLabelText("Message");
    fireEvent.change(box, { target: { value: "line one" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(send).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(send.mock.calls[0]?.[1]).toBe("line one");
  });

  it("starts a new chat, leaving the old one saved", async () => {
    let n = 0;
    const createConversation = vi.fn(async () => ({ ...CONVERSATION, id: `c${++n}` }));
    const messages = async (): Promise<StoredMessage[]> => [
      { role: "user", parts: [{ type: "text", text: "earlier question" }] },
    ];
    render(<App client={fakeClient({ createConversation, messages })} />);
    expect(await screen.findByText("earlier question")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(screen.queryByText("earlier question")).toBeNull());
    expect(await screen.findByRole("region", { name: "Welcome" })).toBeDefined();
    expect(createConversation).toHaveBeenCalled();
    expect(localStorage.getItem("enzo.conversation")).toBe(`c${n}`); // reopening finds the new one
  });

  it("welcomes with suggestions, or a way to connect when nothing is", async () => {
    render(<App client={fakeClient()} />);
    const suggestion = await screen.findByRole("button", { name: "What's in the news today?" });
    fireEvent.click(suggestion);
    expect(screen.getByLabelText<HTMLTextAreaElement>("Message").value).toBe(
      "What's in the news today?",
    );
    cleanup();
    const offline = BRAINS.map((b) => ({ ...b, available: false }));
    render(<App client={fakeClient({ brains: async () => offline })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Connect an account" }));
    expect(screen.getByRole("dialog", { name: "Accounts" })).toBeDefined();
  });

  it("turns Send into Stop while Enzo writes, and keeps what arrived (#46)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const stop = vi.fn(async () => {
      release();
      return true;
    });
    const send = () =>
      (async function* () {
        yield { type: "text" as const, text: "Once upon a time" };
        await gate;
        yield {
          type: "done" as const,
          stop: "cancelled" as const,
          text: "Stopped at your request.",
          steps: 0,
          cost_usd: null,
          brain: "claude-opus",
          model: "m",
          failure: null,
          plan: false,
          usage: NO_USAGE,
        };
      })();
    render(<App client={fakeClient({ send, stop })} />);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "a story" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    expect(stop).toHaveBeenCalledWith("c1");
    expect(await screen.findByText("Stopped.")).toBeDefined();
    expect(screen.getByText("Once upon a time")).toBeDefined();
    expect(screen.getByRole("button", { name: "Send" })).toBeDefined();
  });

  it("marks a refused message not sent and offers to stop the running reply (#46)", async () => {
    const stop = vi.fn(async () => true);
    const send = () =>
      (async function* (): AsyncGenerator<AgentEvent> {
        yield* [];
        throw new GatewayError("/turns failed: HTTP 409: busy", 409);
      })();
    render(<App client={fakeClient({ send, stop })} />);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Not sent")).toBeDefined();
    expect(screen.queryByText(/409/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stop it" }));
    expect(stop).toHaveBeenCalledWith("c1");
  });

  it("says plainly when the gateway is down", async () => {
    render(
      <App client={fakeClient({ health: () => Promise.reject(new Error("ECONNREFUSED")) })} />,
    );
    expect(await screen.findByText(/Gateway unreachable: ECONNREFUSED/)).toBeDefined();
  });
});

describe("images in the web app (#53, ADR 0033)", () => {
  const png = () =>
    new File([new Uint8Array([137, 80, 78, 71])], "shot.png", { type: "image/png" });
  const seeing = BRAINS.map((b) => ({ ...b, vision: b.name === "claude-opus" }));

  it("a picked image is kept first, shown, and sent with the message by id", async () => {
    URL.createObjectURL = vi.fn(() => "blob:preview");
    URL.revokeObjectURL = vi.fn();
    const send = vi.fn(() => events({ type: "error", message: "x" }));
    const client = fakeClient({ send, brains: async () => seeing });
    render(<App client={client} />);
    await screen.findByRole("option", { name: /claude-opus/ });
    fireEvent.change(screen.getByLabelText("Image file"), { target: { files: [png()] } });
    await screen.findByRole("img", { name: "shot.png" });
    expect(client.uploadImage).toHaveBeenCalledWith(expect.any(File), {
      source: "file",
      name: "shot.png",
      type: "image/png",
    });
    expect(screen.queryByText(/can't see images/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "what is it?" } });
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(send).toHaveBeenCalled());
    expect(send.mock.calls[0]).toEqual([
      "c1",
      "what is it?",
      expect.objectContaining({ images: ["img_0123456789abcdef"] }),
    ]);
    expect(await screen.findByText(/what is it\?\s*\[image\]/)).toBeTruthy();
    expect(screen.queryByRole("img", { name: "shot.png" })).toBeNull();
  });

  it("a pasted image is attached; a model that can't see is named before sending", async () => {
    URL.createObjectURL = vi.fn(() => "blob:preview");
    const client = fakeClient(); // no model that sees
    render(<App client={client} />);
    await screen.findByRole("option", { name: /claude-opus/ });
    fireEvent.paste(screen.getByLabelText("Message"), { clipboardData: { files: [png()] } });
    await screen.findByRole("img", { name: "shot.png" });
    expect(client.uploadImage).toHaveBeenCalledWith(expect.any(File), {
      source: "pasted",
      name: "shot.png",
      type: "image/png",
    });
    expect(screen.getByText(CANT_SEE)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove shot.png" }));
    expect(screen.queryByRole("img", { name: "shot.png" })).toBeNull();
  });

  it("an image the gateway refuses is said plainly", async () => {
    URL.createObjectURL = vi.fn(() => "blob:preview");
    URL.revokeObjectURL = vi.fn();
    const uploadImage = vi.fn(async () => {
      throw new GatewayError(
        "/images failed: HTTP 415: That file isn't an image Enzo can read.",
        415,
      );
    });
    render(<App client={fakeClient({ uploadImage })} />);
    await screen.findByRole("option", { name: /claude-opus/ });
    fireEvent.change(screen.getByLabelText("Image file"), { target: { files: [png()] } });
    expect(
      await screen.findByText("Couldn't attach shot.png: That file isn't an image Enzo can read."),
    ).toBeTruthy();
    expect(screen.queryByRole("img", { name: "shot.png" })).toBeNull();
  });
});
