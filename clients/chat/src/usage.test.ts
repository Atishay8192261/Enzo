import { describe, expect, it } from "vitest";
import { NO_USAGE } from "./chat.ts";
import {
  describeStats,
  describeTotals,
  formatClock,
  formatDuration,
  formatSpend,
  formatTokens,
} from "./usage.ts";

const used = {
  ...NO_USAGE,
  calls: 1,
  input_tokens: 1388,
  output_tokens: 81,
  models: ["gpt-6.2-sol"],
};

describe("usage in words (#49)", () => {
  it("reads numbers the way people do", () => {
    expect([0, 812, 1388, 9999, 12_345, 123_456, 1_234_567].map(formatTokens)).toEqual([
      "0",
      "812",
      "1,388",
      "9,999",
      "12.3k",
      "123k",
      "1.2M",
    ]);
    expect([850, 3400, 59_900, 125_000].map(formatDuration)).toEqual([
      "850ms",
      "3.4s",
      "59.9s",
      "2m 05s",
    ]);
    expect(formatClock("2026-11-20T09:05:00Z")).toMatch(/^\d{1,2}[:.]\d{2}/);
  });

  it("says who paid, and never shows an unknown price as free", () => {
    expect(formatSpend({ ...used, cost_usd: 0.0021 })).toBe("$0.0021");
    expect(formatSpend({ ...used, cost_usd: 0.42 })).toBe("$0.42");
    expect(formatSpend({ ...used, cost_usd: 0, plan: true })).toBe("ChatGPT plan");
    expect(formatSpend({ ...used, cost_usd: 0.01, plan: true })).toBe("$0.01 + ChatGPT plan");
    expect(formatSpend({ ...used, cost_usd: null })).toBe("cost unknown");
    expect(formatSpend({ ...used, cost_usd: 0 })).toBe("no charge");
    expect(formatSpend(NO_USAGE)).toBe(""); // nothing was asked of any model
  });

  it("a live reply shows model, first word, total time, tokens and cost", () => {
    const item = {
      role: "assistant" as const,
      text: "hi",
      stats: {
        usage: { ...used, cost_usd: 0.0021, cached_input_tokens: 1000 },
        firstWordMs: 1100,
        totalMs: 3400,
      },
    };
    expect(describeStats(item)).toEqual([
      "gpt-6.2-sol",
      "first word 1.1s",
      "3.4s",
      "1,388 in · 81 out",
      "1,000 cached",
      "$0.0021",
    ]);
  });

  it("a reopened reply shows the models' own time instead of waiting time", () => {
    const item = {
      role: "assistant" as const,
      text: "hi",
      stats: { usage: { ...used, model_ms: 5017, plan: true } },
    };
    expect(describeStats(item)).toEqual([
      "gpt-6.2-sol",
      "5.0s model time",
      "1,388 in · 81 out",
      "ChatGPT plan",
    ]);
    expect(describeStats({ role: "assistant", text: "no stats" })).toEqual([]);
    // a reply no model was asked for (no account connected) says only that
    expect(describeStats({ role: "notice", text: "x", stats: { usage: NO_USAGE } })).toEqual([]);
    expect(
      describeStats({ role: "notice", text: "x", stats: { usage: NO_USAGE, totalMs: 40 } }),
    ).toEqual([]);
  });

  it("sums a chat up in one line", () => {
    expect(describeTotals({ ...used, cost_usd: 0.04 })).toBe("1,469 tokens · $0.04");
    expect(describeTotals(NO_USAGE)).toBe("");
  });
});
