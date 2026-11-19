import type { AccountsClient, ChatClient } from "@enzo/chat";
import type { AgentEvent, Brain, Conversation, StoredMessage } from "@enzo/sdk";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App.tsx";

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

function fakeClient(
  overrides: Partial<ChatClient & AccountsClient> = {},
): ChatClient & AccountsClient {
  return {
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
    health: async () => ({ status: "ok", version: "1", env: "test" }),
    brains: async () => BRAINS,
    createConversation: async () => CONVERSATION,
    conversation: async () => CONVERSATION,
    messages: async (): Promise<StoredMessage[]> => [],
    chooseBrain: vi.fn(async () => {}),
    send: () => events(),
    resume: () => events(),
    decide: vi.fn(async () => {}),
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
        }),
    });
    render(<App client={client} />);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/No model account is connected yet/);
    expect(screen.queryByText(/\/login/)).toBeNull();
    expect(screen.getByText("no charge")).toBeDefined();
    const accounts = screen.getByText("Accounts", { selector: "summary" }).closest("details");
    expect(accounts?.open).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Open Accounts" }));
    expect(accounts?.open).toBe(true);
  });

  it("says plainly when the gateway is down", async () => {
    render(
      <App client={fakeClient({ health: () => Promise.reject(new Error("ECONNREFUSED")) })} />,
    );
    expect(await screen.findByText(/Gateway unreachable: ECONNREFUSED/)).toBeDefined();
  });
});
