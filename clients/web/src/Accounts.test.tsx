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
    label: null,
  };
}

function fake(
  overrides: Partial<AccountsClient> = {},
  start: ProviderConnection[] = [row("anthropic", true), row("xai", false)],
): AccountsClient {
  let rows = start;
  return {
    connections: async () => rows,
    connect: vi.fn(async (provider: string) => {
      rows = rows.map((r) => (r.provider === provider ? row(provider, true) : r));
    }),
    disconnect: vi.fn(async (provider: string) => {
      rows = rows.map((r) => (r.provider === provider ? row(provider, false) : r));
      return { remote_sign_out: null };
    }),
    checkConnection: async () => ({ ok: true, detail: null }),
    startChatGPTSignIn: vi.fn(async () => ({
      login_id: "l1",
      url: "http://127.0.0.1:8787/auth/chatgpt/l1",
    })),
    chatGPTSignIn: async () => {
      rows = rows.map((r) =>
        r.provider === "openai"
          ? { ...r, connected: true, method: "chatgpt", label: "ada@example.com" }
          : r,
      );
      return {
        status: "connected" as const,
        message: "Signed in with ChatGPT as ada@example.com.",
        first_time: true,
        usage_url: "https://chatgpt.com/settings/usage",
      };
    },
    ...overrides,
  };
}

describe("Accounts", () => {
  it("continues with ChatGPT in a new tab and shows the signed-in account", async () => {
    const tab = { opener: {} as unknown, location: { href: "about:blank" } };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    const openai = { ...row("openai", false), methods: ["chatgpt", "api_key"] };
    render(<Accounts client={fake({}, [openai])} />);
    fireEvent.click(await screen.findByRole("button", { name: "Continue with ChatGPT" }));
    expect(open).toHaveBeenCalledWith("about:blank", "_blank"); // inside the click: not blocked
    expect(tab.opener).toBeNull(); // the new tab cannot reach back into Enzo
    await waitFor(() => expect(tab.location.href).toBe("http://127.0.0.1:8787/auth/chatgpt/l1"), {
      timeout: 3000,
    });
    await screen.findByText(/You're using your ChatGPT plan/, {}, { timeout: 3000 });
    await waitFor(() =>
      expect(document.body.textContent).toContain("Connected (ChatGPT plan, ada@example.com"),
    );
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Manage usage" }).getAttribute("href")).toBe(
      "https://chatgpt.com/settings/usage",
    );
    open.mockRestore();
  });

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
