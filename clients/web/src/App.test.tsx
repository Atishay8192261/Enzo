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
  { name: "claude-opus", provider: "anthropic", model: "claude-opus-5-5", priced: true },
  { name: "grok", provider: "xai", model: "grok-4.7", priced: false },
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
    disconnect: vi.fn(async () => {}),
    checkConnection: async () => ({ ok: true, detail: null }),
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
          { type: "done", stop: "final_answer", text: "Paris", steps: 1, cost_usd: 0.0021 },
        ),
    });
    render(<App client={client} />);
    const input = await screen.findByLabelText("Message");
    expect(screen.getByRole("option", { name: /grok · grok-4.7 \(cost unknown\)/ })).toBeDefined();
    fireEvent.change(input, { target: { value: "Capital of France?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Paris")).toBeDefined();
    expect(screen.getByText("Capital of France?")).toBeDefined();
    expect(screen.getByText("cost $0.0021")).toBeDefined();
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
            arguments: { to: "bob@example.com", body: "hi" },
          },
          { type: "done", stop: "approval_pending", text: "Waiting", steps: 1, cost_usd: null },
        ),
      resume: () =>
        events({ type: "done", stop: "final_answer", text: "Sent.", steps: 2, cost_usd: null }),
    });
    render(<App client={client} />);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "email bob" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const card = await screen.findByRole("region", { name: "Approval needed" });
    expect(card.textContent).toContain('"to": "bob@example.com"');
    expect(card.textContent).toContain("send_email");
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

  it("stores the brain the user picks", async () => {
    const chooseBrain = vi.fn(async () => {});
    render(<App client={fakeClient({ chooseBrain })} />);
    await screen.findByLabelText("Message");
    fireEvent.change(screen.getByLabelText("Brain"), { target: { value: "grok" } });
    await waitFor(() => expect(chooseBrain).toHaveBeenCalledWith("c1", "grok"));
  });

  it("says plainly when the gateway is down", async () => {
    render(
      <App client={fakeClient({ health: () => Promise.reject(new Error("ECONNREFUSED")) })} />,
    );
    expect(await screen.findByText(/Gateway unreachable: ECONNREFUSED/)).toBeDefined();
  });
});
