import { type GatewayClient, GatewayError, type ProviderConnection } from "@enzo/sdk";

/** What the account screens need from the gateway. */
export type AccountsClient = Pick<
  GatewayClient,
  | "connections"
  | "connect"
  | "disconnect"
  | "checkConnection"
  | "startChatGPTSignIn"
  | "chatGPTSignIn"
>;

/** Where users review and limit what Enzo uses of their ChatGPT plan (OpenAI's guidance). */
export const CHATGPT_USAGE_URL = "https://chatgpt.com/settings/usage";

/** One plain line per provider: state, how to fix it, and which models it unlocks. */
export function describeConnection(c: ProviderConnection): string {
  const how = [
    methodName(c.method),
    c.label,
    c.connected_at ? `since ${c.connected_at.slice(0, 10)}` : null,
  ]
    .filter(Boolean)
    .join(", ");
  const login = c.methods.includes("chatgpt")
    ? "/login chatgpt (your ChatGPT plan) or /login openai (API key)"
    : `/login ${c.provider}`;
  const state = c.connected ? `connected (${how})` : `not connected: ${login}`;
  const unlocks = c.models.length > 0 ? c.models.join(", ") : (c.purpose ?? c.tools.join(", "));
  return `${c.provider}: ${state} · ${unlocks}`;
}

export function methodName(method: string | null): string {
  if (method === "api_key") return "API key";
  if (method === "chatgpt") return "ChatGPT plan";
  if (method === "environment") return "environment variable";
  return method ?? "unknown";
}

export interface SignInResult {
  ok: boolean;
  text: string;
  /** On success: where the user reviews and limits Enzo's use of their plan. */
  usageUrl?: string;
}

export interface SignInOptions {
  /** Add another ChatGPT account instead of signing in to the one Enzo knows. */
  newAccount?: boolean;
  /** Open the sign-in page in the user's browser. Failing is fine: the link is shown too. */
  open(url: string): void | Promise<void>;
  /** Called once the page is open, with the link to use if no browser appeared. */
  waiting?(url: string): void;
  /** Stop waiting (e.g. the user cancelled). */
  signal?: AbortSignal;
  pollMs?: number;
  timeoutMs?: number;
  sleep?(ms: number): Promise<void>;
}

/**
 * Sign in with ChatGPT from any surface: start, open the browser, then wait for the gateway to
 * hear back from OpenAI. Never throws: the result says what happened in plain words.
 */
export async function signInWithChatGPT(
  client: Pick<AccountsClient, "startChatGPTSignIn" | "chatGPTSignIn">,
  options: SignInOptions,
): Promise<SignInResult> {
  const sleep = options.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  const pollMs = options.pollMs ?? 1000;
  const deadline = Date.now() + (options.timeoutMs ?? 10 * 60_000);
  try {
    const { login_id, url } = await client.startChatGPTSignIn(options.newAccount ?? false);
    try {
      await options.open(url);
    } catch {
      // No browser could be opened: the link below still works.
    }
    options.waiting?.(url);
    while (Date.now() < deadline) {
      if (options.signal?.aborted) return { ok: false, text: "ChatGPT sign-in cancelled." };
      await sleep(pollMs);
      const attempt = await client.chatGPTSignIn(login_id);
      if (attempt.status === "connected") {
        const welcome = attempt.first_time
          ? "You're using your ChatGPT plan: Enzo's GPT replies now count toward it. "
          : "";
        return {
          ok: true,
          text: `${welcome}${attempt.message ?? "Signed in with ChatGPT."}`,
          usageUrl: attempt.usage_url,
        };
      }
      if (attempt.status === "failed") {
        return { ok: false, text: attempt.message ?? "ChatGPT sign-in failed." };
      }
    }
    return { ok: false, text: "The ChatGPT sign-in timed out. Start again: /login chatgpt" };
  } catch (e: unknown) {
    if (e instanceof GatewayError && e.status === 404) {
      return { ok: false, text: "That ChatGPT sign-in expired. Start again: /login chatgpt" };
    }
    return { ok: false, text: e instanceof Error ? e.message : String(e) };
  }
}
