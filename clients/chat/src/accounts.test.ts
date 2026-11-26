import { type ChatGPTSignInStatus, GatewayError, type ProviderConnection } from "@enzo/sdk";
import { describe, expect, it, vi } from "vitest";
import { describeConnection, signInWithChatGPT, signInWithGoogle } from "./accounts.ts";

const base: ProviderConnection = {
  provider: "anthropic",
  connected: false,
  method: null,
  connected_at: null,
  methods: ["api_key"],
  models: ["claude-opus-5-5", "claude-sonnet-5-5"],
  tools: [],
  purpose: null,
  label: null,
};

describe("accounts", () => {
  it("describes connected and unconnected providers", () => {
    expect(describeConnection(base)).toBe(
      "anthropic: not connected: /login anthropic · claude-opus-5-5, claude-sonnet-5-5",
    );
    const on = {
      ...base,
      connected: true,
      method: "api_key",
      connected_at: "2026-11-17T21:00:00Z",
    };
    expect(describeConnection(on)).toBe(
      "anthropic: connected (API key, since 2026-11-17) · claude-opus-5-5, claude-sonnet-5-5",
    );
    const env = { ...base, connected: true, method: "environment" };
    expect(describeConnection(env)).toBe(
      "anthropic: connected (environment variable) · claude-opus-5-5, claude-sonnet-5-5",
    );
    const search = {
      ...base,
      provider: "tavily",
      models: [],
      tools: ["web_search"],
      purpose: "optional: raises web search limits (search works without it)",
    };
    expect(describeConnection(search)).toBe(
      "tavily: not connected: /login tavily · optional: raises web search limits (search works without it)",
    );
  });
});

describe("ChatGPT accounts", () => {
  it("names the plan sign-in and how to connect", () => {
    const openai = {
      ...base,
      provider: "openai",
      methods: ["chatgpt", "api_key"],
      models: ["gpt"],
    };
    expect(describeConnection(openai)).toBe(
      "openai: not connected: /login chatgpt (your ChatGPT plan) or /login openai (API key) · gpt",
    );
    const signedIn = { ...openai, connected: true, method: "chatgpt", label: "ada@example.com" };
    expect(describeConnection(signedIn)).toBe(
      "openai: connected (ChatGPT plan, ada@example.com) · gpt",
    );
  });
});

const STARTED = { login_id: "l1", url: "http://127.0.0.1:8787/auth/chatgpt/l1" };

function attempt(status: ChatGPTSignInStatus["status"], over: Partial<ChatGPTSignInStatus> = {}) {
  return {
    status,
    message: status === "connected" ? "Signed in with ChatGPT as ada@example.com." : null,
    first_time: false,
    usage_url: "https://chatgpt.com/settings/usage",
    ...over,
  };
}

const instant = { sleep: async () => {}, pollMs: 1 };

describe("signInWithChatGPT", () => {
  it("opens the page, waits for the browser, then reports the account", async () => {
    const replies = [
      attempt("pending"),
      attempt("pending"),
      attempt("connected", { first_time: true }),
    ];
    const client = {
      startChatGPTSignIn: vi.fn(async () => STARTED),
      chatGPTSignIn: vi.fn(async () => replies.shift() ?? attempt("connected")),
    };
    const open = vi.fn();
    const waiting = vi.fn();
    const result = await signInWithChatGPT(client, { open, waiting, ...instant });
    expect(open).toHaveBeenCalledWith(STARTED.url);
    expect(waiting).toHaveBeenCalledWith(STARTED.url);
    expect(client.chatGPTSignIn).toHaveBeenCalledTimes(3);
    expect(result).toEqual({
      ok: true,
      text: "You're using your ChatGPT plan: Enzo's GPT replies now count toward it. Signed in with ChatGPT as ada@example.com.",
      usageUrl: "https://chatgpt.com/settings/usage",
    });
  });

  it("keeps waiting when no browser opened: the link is shown instead", async () => {
    const client = {
      startChatGPTSignIn: async () => STARTED,
      chatGPTSignIn: async () => attempt("connected"),
    };
    const waiting = vi.fn();
    const open = () => {
      throw new Error("no browser");
    };
    const result = await signInWithChatGPT(client, { open, waiting, ...instant });
    expect(waiting).toHaveBeenCalledWith(STARTED.url);
    expect(result.ok).toBe(true);
  });

  it("reports a failed, expired, timed out or cancelled sign-in in plain words", async () => {
    const failed = await signInWithChatGPT(
      {
        startChatGPTSignIn: async () => STARTED,
        chatGPTSignIn: async () => attempt("failed", { message: "The sign-in was cancelled." }),
      },
      { open: () => {}, ...instant },
    );
    expect(failed).toEqual({ ok: false, text: "The sign-in was cancelled." });

    const expired = await signInWithChatGPT(
      {
        startChatGPTSignIn: async () => STARTED,
        chatGPTSignIn: async () => {
          throw new GatewayError("gone", 404);
        },
      },
      { open: () => {}, ...instant },
    );
    expect(expired.text).toBe("That ChatGPT sign-in expired. Start again: /login chatgpt");

    const slow = await signInWithChatGPT(
      { startChatGPTSignIn: async () => STARTED, chatGPTSignIn: async () => attempt("pending") },
      { open: () => {}, timeoutMs: 0, ...instant },
    );
    expect(slow.text).toBe("The ChatGPT sign-in timed out. Start again: /login chatgpt");

    const stop = new AbortController();
    stop.abort();
    const cancelled = await signInWithChatGPT(
      { startChatGPTSignIn: async () => STARTED, chatGPTSignIn: async () => attempt("pending") },
      { open: () => {}, signal: stop.signal, ...instant },
    );
    expect(cancelled.text).toBe("ChatGPT sign-in cancelled.");
  });

  it("passes newAccount through", async () => {
    const startChatGPTSignIn = vi.fn(async () => STARTED);
    await signInWithChatGPT(
      { startChatGPTSignIn, chatGPTSignIn: async () => attempt("connected") },
      { open: () => {}, newAccount: true, ...instant },
    );
    expect(startChatGPTSignIn).toHaveBeenCalledWith(true);
  });
});

const GOOGLE_STARTED = {
  login_id: "g1",
  url: "http://127.0.0.1:8787/auth/google/g1",
  needs_client: false,
  message: null,
};

describe("Google accounts", () => {
  it("names the sign-in and both ways to connect", () => {
    const google = {
      ...base,
      provider: "google",
      methods: ["google"],
      models: [],
      tools: ["gmail", "calendar"],
      purpose: "Gmail and Google Calendar: read, send and schedule (sign in with Google)",
    };
    expect(describeConnection(google)).toBe(
      "google: not connected: /login google (or /login google read: reading only) · Gmail and Google Calendar: read, send and schedule (sign in with Google)",
    );
    const on = {
      ...google,
      connected: true,
      method: "google",
      label: "ada@example.com",
      purpose: "Gmail (read, send, drafts) and Google Calendar (read, edit)",
    };
    expect(describeConnection(on)).toBe(
      "google: connected (Google sign-in, ada@example.com) · Gmail (read, send, drafts) and Google Calendar (read, edit)",
    );
  });

  it("asks for the user's own client when none is set up, without opening anything", async () => {
    const open = vi.fn();
    const googleSignIn = vi.fn();
    const result = await signInWithGoogle(
      {
        startGoogleSignIn: async () => ({
          login_id: null,
          url: null,
          needs_client: true,
          message: "Google needs your own OAuth client first.",
        }),
        googleSignIn,
      },
      { open, ...instant },
    );
    expect(result).toEqual({
      ok: false,
      needsClient: true,
      text: "Google needs your own OAuth client first.",
    });
    expect(open).not.toHaveBeenCalled();
    expect(googleSignIn).not.toHaveBeenCalled();
  });

  it("opens Google's page, waits, and reports what was allowed", async () => {
    const replies = [
      { status: "pending" as const, message: null },
      { status: "connected" as const, message: "Connected Google as ada@example.com." },
    ];
    const startGoogleSignIn = vi.fn(async () => GOOGLE_STARTED);
    const open = vi.fn();
    const result = await signInWithGoogle(
      {
        startGoogleSignIn,
        googleSignIn: async () => replies.shift() ?? { status: "pending", message: null },
      },
      { open, access: "read", ...instant },
    );
    expect(startGoogleSignIn).toHaveBeenCalledWith("read");
    expect(open).toHaveBeenCalledWith(GOOGLE_STARTED.url);
    expect(result).toEqual({ ok: true, text: "Connected Google as ada@example.com." });
  });

  it("says why it did not connect", async () => {
    const failed = await signInWithGoogle(
      {
        startGoogleSignIn: async () => GOOGLE_STARTED,
        googleSignIn: async () => ({
          status: "failed",
          message: "The Google sign-in was cancelled.",
        }),
      },
      { open: () => {}, ...instant },
    );
    expect(failed).toEqual({ ok: false, text: "The Google sign-in was cancelled." });
    const expired = await signInWithGoogle(
      {
        startGoogleSignIn: async () => GOOGLE_STARTED,
        googleSignIn: async () => {
          throw new GatewayError("gone", 404);
        },
      },
      { open: () => {}, ...instant },
    );
    expect(expired.text).toBe("That Google sign-in expired. Start again: /login google");
  });
});
