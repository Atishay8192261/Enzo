import { type Brain, TIERS, type Tier } from "@enzo/sdk";

export const TIER_HINT: Record<Tier, string> = {
  fast: "cheapest, quickest",
  balanced: "everyday default",
  powerful: "hardest tasks",
};

/** The catalog as plain lines, grouped by tier: ★ marks Enzo's pick, and what blocks a model. */
export function describeCatalog(brains: Brain[], current: string | null): string {
  const lines = [`Now: ${describeChoice(current, brains)}`];
  for (const tier of TIERS) {
    const inTier = brains.filter((b) => b.tier === tier);
    if (inTier.length === 0) continue;
    lines.push(`${tier} (${TIER_HINT[tier]}):`);
    for (const b of inTier) {
      const mark = b.name === current ? "›" : " ";
      const star = b.recommended ? "★" : " ";
      const state = b.available ? "" : ` · needs /login ${b.provider}`;
      const cost = b.priced ? "" : " · cost unknown";
      lines.push(`${mark} ${star} ${b.name} (${b.model})${cost}${state}`);
    }
  }
  lines.push("Choose: /model <name>, /model <fast|balanced|powerful>, or /model auto");
  return lines.join("\n");
}

/** What a conversation is set to, in words. */
export function describeChoice(choice: string | null, brains: Brain[]): string {
  if (choice === null) return "auto (smart routing across your connected accounts)";
  if ((TIERS as readonly string[]).includes(choice)) return `any ${choice} model`;
  const brain = brains.find((b) => b.name === choice);
  return brain ? `${brain.name} (${brain.model})` : choice;
}

/**
 * Validate a `/model` argument against the catalog: a model name, a tier, or `auto`.
 * A model whose provider is not connected is refused with the command that fixes it.
 */
export function resolveChoice(
  arg: string,
  brains: Brain[],
): { choice: string | null } | { error: string } {
  if (arg === "auto") return { choice: null };
  if ((TIERS as readonly string[]).includes(arg)) {
    if (!brains.some((b) => b.tier === arg && b.available)) {
      return { error: `No connected account has a ${arg} model yet. See /models.` };
    }
    return { choice: arg };
  }
  const brain = brains.find((b) => b.name === arg);
  if (!brain) return { error: `Unknown model: ${arg}. See /models.` };
  if (!brain.available) {
    return {
      error: `${brain.name} needs your ${brain.provider} account: /login ${brain.provider}`,
    };
  }
  return { choice: brain.name };
}
