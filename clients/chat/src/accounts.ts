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
  return method === "api_key" ? "API key" : (method ?? "unknown");
}

/**
 * A key typed on a command line is visible on screen and may land in shell or chat history.
 * Returns the provider for `/login <provider>`, or an error for anything that carries more.
 */
export function parseLogin(line: string): { provider: string } | { error: string } | null {
  const [command, provider, ...rest] = line.trim().split(/\s+/);
  if (command !== "/login") return null;
  if (!provider) return { error: "Usage: /login <provider>. See /connections for providers." };
  if (rest.length > 0) {
    return {
      error: `Never type a key on the command line. Run /login ${provider} and paste it at the hidden prompt.`,
    };
  }
  return { provider };
}
