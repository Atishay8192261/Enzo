import type { Usage } from "@enzo/sdk";
import type { ChatItem } from "./chat.ts";

/** Token counts people can read: 812, 1,388, 12.3k, 1.2M. */
export function formatTokens(n: number): string {
  if (n < 10_000) return n.toLocaleString("en-US");
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 100_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** A duration: 850ms, 3.4s, 2m 05s. */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

/** Money for the chat: $0.0021, $0.42. */
export function formatMoney(usd: number): string {
  return `$${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}`;
}

/** Who paid, in words: "$0.0021", "ChatGPT plan", "$0.01 + ChatGPT plan", "cost unknown". */
export function formatSpend(usage: Usage): string {
  const plan = usage.plan ? "ChatGPT plan" : "";
  if (usage.cost_usd === null) return plan ? `${plan} + cost unknown` : "cost unknown";
  if (usage.cost_usd === 0) return plan || (usage.calls > 0 ? "no charge" : "");
  return plan ? `${formatMoney(usage.cost_usd)} + ${plan}` : formatMoney(usage.cost_usd);
}

/** The time of day a message was sent, as the user's clock shows it: "14:02". */
export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * The facts under a reply (#49), one short part each:
 * model · first word 1.1s · 3.4s · 1,388 in · 81 out · $0.0021
 * A reopened chat has no first word or waiting time, only the models' own time.
 */
export function describeStats(item: ChatItem): string[] {
  const stats = item.stats;
  // No model was asked (nothing connected, or stopped first): there is nothing to report.
  if (!stats || stats.usage.calls === 0) return [];
  const u = stats.usage;
  const parts: string[] = [];
  const model = u.models.at(-1) ?? item.by;
  if (model) parts.push(model);
  if (stats.firstWordMs !== undefined)
    parts.push(`first word ${formatDuration(stats.firstWordMs)}`);
  if (stats.totalMs !== undefined) parts.push(formatDuration(stats.totalMs));
  else if (u.model_ms > 0) parts.push(`${formatDuration(u.model_ms)} model time`);
  if (u.calls > 0) {
    parts.push(`${formatTokens(u.input_tokens)} in · ${formatTokens(u.output_tokens)} out`);
    if (u.cached_input_tokens > 0) parts.push(`${formatTokens(u.cached_input_tokens)} cached`);
  }
  const spend = formatSpend(u);
  if (spend) parts.push(spend);
  return parts;
}

/** A chat's totals in one line: "12.3k tokens · $0.04" (nothing before the first reply). */
export function describeTotals(u: Usage): string {
  if (u.calls === 0) return "";
  const tokens = `${formatTokens(u.input_tokens + u.output_tokens)} tokens`;
  const spend = formatSpend(u);
  return spend ? `${tokens} · ${spend}` : tokens;
}
