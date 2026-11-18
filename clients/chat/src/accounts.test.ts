import type { ProviderConnection } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import { describeConnection } from "./accounts.ts";

const base: ProviderConnection = {
  provider: "anthropic",
  connected: false,
  method: null,
  connected_at: null,
  methods: ["api_key"],
  models: ["claude-opus-5-5", "claude-sonnet-5-5"],
  tools: [],
  purpose: null,
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
