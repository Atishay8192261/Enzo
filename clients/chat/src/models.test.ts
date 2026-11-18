import type { Brain } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import { describeCatalog, describeChoice, resolveChoice } from "./models.ts";

function brain(name: string, over: Partial<Brain> = {}): Brain {
  return {
    name,
    provider: "anthropic",
    model: `${name}-model`,
    priced: true,
    tier: "balanced",
    recommended: false,
    note: null,
    available: true,
    ...over,
  };
}

const BRAINS = [
  brain("sonnet", { recommended: true }),
  brain("opus", { tier: "powerful", recommended: true }),
  brain("grok", { tier: "powerful", provider: "xai", available: false, priced: false }),
];

describe("model catalog", () => {
  it("groups by tier, stars recommendations and says what is missing", () => {
    expect(describeCatalog(BRAINS, "opus").split("\n")).toEqual([
      "Now: opus (opus-model)",
      "balanced (everyday default):",
      "  ★ sonnet (sonnet-model)",
      "powerful (hardest tasks):",
      "› ★ opus (opus-model)",
      "    grok (grok-model) · cost unknown · needs /login xai",
      "Choose: /model <name>, /model <fast|balanced|powerful>, or /model auto",
    ]);
  });

  it("describes auto, tiers and models", () => {
    expect(describeChoice(null, BRAINS)).toContain("auto");
    expect(describeChoice("powerful", BRAINS)).toBe("any powerful model");
    expect(describeChoice("sonnet", BRAINS)).toBe("sonnet (sonnet-model)");
  });

  it("resolves names, tiers and auto, and refuses what cannot answer", () => {
    expect(resolveChoice("auto", BRAINS)).toEqual({ choice: null });
    expect(resolveChoice("powerful", BRAINS)).toEqual({ choice: "powerful" });
    expect(resolveChoice("opus", BRAINS)).toEqual({ choice: "opus" });
    expect(resolveChoice("grok", BRAINS)).toEqual({
      error: "grok needs your xai account: /login xai",
    });
    expect(resolveChoice("fast", BRAINS)).toEqual({
      error: "No connected account has a fast model yet. See /models.",
    });
    expect(resolveChoice("ghost", BRAINS)).toEqual({ error: "Unknown model: ghost. See /models." });
  });
});
