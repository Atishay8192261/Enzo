import type { GatewayClient, ProviderConnection } from "@enzo/sdk";

/** What the account screens need from the gateway. */
export type AccountsClient = Pick<
  GatewayClient,
  "connections" | "connect" | "disconnect" | "checkConnection"
>;

/** One plain line per provider: state, how to fix it, and which models it unlocks. */
export function describeConnection(c: ProviderConnection): string {
  const state = c.connected
    ? `connected (${methodName(c.method)}${c.connected_at ? `, since ${c.connected_at.slice(0, 10)}` : ""})`
    : `not connected: /login ${c.provider}`;
  return `${c.provider}: ${state} · ${c.models.join(", ")}`;
}

export function methodName(method: string | null): string {
  if (method === "api_key") return "API key";
  if (method === "environment") return "environment variable";
  return method ?? "unknown";
}
