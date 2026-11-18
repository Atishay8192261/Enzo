import type { AccountsClient } from "@enzo/chat";
import type { ProviderConnection } from "@enzo/sdk";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Accounts } from "./Accounts.tsx";

afterEach(cleanup);

const KEY = "fake-xai-000000000000";

function row(provider: string, connected: boolean): ProviderConnection {
  return {
    provider,
    connected,
    method: connected ? "api_key" : null,
    connected_at: connected ? "2026-11-17T20:00:00Z" : null,
    methods: ["api_key"],
    models: [`${provider}-model`],
    tools: [],
    purpose: null,
  };
}

function fake(overrides: Partial<AccountsClient> = {}): AccountsClient {
  let rows = [row("anthropic", true), row("xai", false)];
  return {
    connections: async () => rows,
    connect: vi.fn(async (provider: string) => {
      rows = rows.map((r) => (r.provider === provider ? row(provider, true) : r));
    }),
    disconnect: vi.fn(async (provider: string) => {
      rows = rows.map((r) => (r.provider === provider ? row(provider, false) : r));
    }),
    checkConnection: async () => ({ ok: true, detail: null }),
    ...overrides,
  };
}

describe("Accounts", () => {
  it("connects with a password field and clears it at once", async () => {
    const client = fake();
    render(<Accounts client={client} />);
    const field = await screen.findByLabelText<HTMLInputElement>("xai API key");
    expect(field.type).toBe("password");
    expect(field.autocomplete).toBe("off");
    fireEvent.change(field, { target: { value: `  ${KEY}\n` } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(field.value).toBe("");
    await screen.findByText("Connected xai.");
    expect(client.connect).toHaveBeenCalledWith("xai", KEY);
    await waitFor(() => expect(screen.queryByLabelText("xai API key")).toBeNull());
    expect(document.body.textContent).not.toContain(KEY);
  });

  it("shows a rejected key as an alert, without the key", async () => {
    const connect = vi.fn(async () => {
      throw new Error("/connections/xai failed: HTTP 422: xai rejected this key");
    });
    render(<Accounts client={fake({ connect })} />);
    const field = await screen.findByLabelText("xai API key");
    fireEvent.change(field, { target: { value: KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect((await screen.findByRole("alert")).textContent).toContain("xai rejected this key");
    expect(document.body.textContent).not.toContain(KEY);
    expect(screen.getByLabelText("xai API key")).toBeTruthy(); // still not connected
  });

  it("checks and disconnects a connected account", async () => {
    const checkConnection = vi.fn(async () => ({
      ok: false,
      detail: "anthropic rejected this key",
    }));
    const client = fake({ checkConnection });
    render(<Accounts client={client} />);
    await screen.findByText(/Connected \(API key, since 2026-11-17\)/);
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "anthropic: anthropic rejected this key",
    );
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await screen.findByText("Disconnected anthropic. Its key was deleted.");
    expect(client.disconnect).toHaveBeenCalledWith("anthropic");
    await screen.findByLabelText("anthropic API key");
  });
});
