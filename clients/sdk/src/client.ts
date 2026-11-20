import { type AgentEvent, toAgentEvent, type Usage } from "./events.ts";
import { parseSse } from "./sse.ts";

export interface Health {
  status: "ok";
  version: string;
  env: string;
}

export type Tier = "fast" | "balanced" | "powerful";
export const TIERS: readonly Tier[] = ["fast", "balanced", "powerful"];

export interface Brain {
  name: string;
  provider: string;
  model: string;
  priced: boolean;
  tier: Tier;
  /** Enzo's pick for this provider in this tier. */
  recommended: boolean;
  note: string | null;
  /** The provider is connected, so this model can answer now. */
  available: boolean;
}

export interface Conversation {
  id: string;
  kind: "main" | "side" | "external";
  title: string | null;
  brain: string | null;
  /** Approval cards still waiting for the user, so a reopened app shows them (#39). */
  pending_approvals?: ApprovalCard[];
  /** A reply is being written right now (another window, or one whose stream was lost). */
  running?: boolean;
}

export interface ApprovalCard {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  summary: string | null;
  detail: string | null;
}

export interface StoredMessage {
  role: "system" | "user" | "assistant" | "tool";
  parts: StoredPart[];
}

/** One part of a stored message: text, a tool call (with its step in plain words) or a result. */
export interface StoredPart {
  type: string;
  text?: string;
  name?: string;
  content?: string;
  /** A tool call's id; a result names the call it answers with `call_id`. */
  id?: string;
  call_id?: string;
  /** A tool call's step in plain words, e.g. "Reading plan.md" (#40). */
  label?: string;
  /** How long a tool call took (#49). */
  latency_ms?: number;
  is_error?: boolean;
}

/** What a chat used, in total and for each of the user's messages, in order (#49). */
export interface ConversationUsage {
  total: Usage;
  turns: Usage[];
}

/** A provider account the user can connect. Never carries the secret. */
export interface ProviderConnection {
  provider: string;
  connected: boolean;
  method: string | null;
  connected_at: string | null;
  methods: string[];
  models: string[];
  /** Service accounts (e.g. web search) unlock tools instead of models. */
  tools: string[];
  purpose: string | null;
  /** Tells accounts apart, e.g. the email of the ChatGPT account that is signed in. */
  label: string | null;
}

/** A Sign in with ChatGPT attempt: open `url` in the browser, then poll by `login_id`. */
export interface ChatGPTSignInStart {
  login_id: string;
  /** A local address on the gateway that forwards to OpenAI's sign-in page. */
  url: string;
}

export interface ChatGPTSignInStatus {
  status: "pending" | "connected" | "failed";
  message: string | null;
  /** The first sign-in on this computer: say once that replies now use the plan. */
  first_time: boolean;
  /** Where the user reviews and limits what Enzo uses of their ChatGPT plan. */
  usage_url: string;
}

export interface Disconnected {
  /** For a sign-in: whether the provider confirmed the session ended. Null for a key. */
  remote_sign_out: "confirmed" | "not_confirmed" | null;
}

export interface ConnectionCheck {
  ok: boolean;
  detail: string | null;
}

export interface TurnContext {
  timezone?: string;
  device?: string;
}

export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

/**
 * The gateway could not be reached at all (it is not running, or the network is down), as
 * opposed to answering with an error. Clients say so plainly and retry (#44).
 */
export class GatewayUnreachableError extends GatewayError {
  constructor(
    readonly url: string,
    cause?: unknown,
  ) {
    super(`Can't reach Enzo at ${url.replace(/^https?:\/\//, "")}`);
    this.name = "GatewayUnreachableError";
    this.cause = cause;
  }
}

/** Any fetch-shaped function. Not `typeof fetch`: runtimes (Bun) add members a wrapper lacks. */
export type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface GatewayClientOptions {
  baseUrl: string;
  token?: string;
  fetch?: Fetcher;
}

export class GatewayClient {
  readonly #baseUrl: string;
  readonly #token: string | undefined;
  readonly #fetch: Fetcher;

  constructor({ baseUrl, token, fetch: fetchImpl = globalThis.fetch }: GatewayClientOptions) {
    this.#baseUrl = baseUrl.replace(/\/+$/, "");
    this.#token = token;
    // Browsers throw "Illegal invocation" if fetch runs with `this` bound to another object.
    this.#fetch = (input, init) => fetchImpl(input, init);
  }

  async health(signal?: AbortSignal): Promise<Health> {
    const body = await this.#json("GET", "/healthz", undefined, signal);
    if (!isHealth(body)) throw new GatewayError("healthz returned an unexpected body");
    return body;
  }

  brains(): Promise<Brain[]> {
    return this.#json("GET", "/brains") as Promise<Brain[]>;
  }

  createConversation(kind: Conversation["kind"] = "main", title?: string): Promise<Conversation> {
    return this.#json("POST", "/conversations", {
      kind,
      ...(title ? { title } : {}),
    }) as Promise<Conversation>;
  }

  conversation(id: string): Promise<Conversation> {
    return this.#json("GET", `/conversations/${enc(id)}`) as Promise<Conversation>;
  }

  /** Pin a model or a tier (`fast`, `balanced`, `powerful`); `null` is automatic routing. */
  async chooseBrain(id: string, brain: string | null): Promise<void> {
    await this.#json("PUT", `/conversations/${enc(id)}/brain`, { brain });
  }

  messages(id: string): Promise<StoredMessage[]> {
    return this.#json("GET", `/conversations/${enc(id)}/messages`) as Promise<StoredMessage[]>;
  }

  /** What this chat used: tokens, cost or plan, models, time (#49). */
  usage(id: string): Promise<ConversationUsage> {
    return this.#json("GET", `/conversations/${enc(id)}/usage`) as Promise<ConversationUsage>;
  }

  /** Stop the reply being written in this conversation (#46). False: none was running. */
  async stop(id: string): Promise<boolean> {
    const out = (await this.#json("POST", `/conversations/${enc(id)}/stop`)) as {
      stopping: boolean;
    };
    return out.stopping;
  }

  async decide(id: string, approvalId: string, approved: boolean): Promise<void> {
    await this.#json("POST", `/conversations/${enc(id)}/approvals/${enc(approvalId)}`, {
      approved,
    });
  }

  connections(): Promise<ProviderConnection[]> {
    return this.#json("GET", "/connections") as Promise<ProviderConnection[]>;
  }

  /** Checks the key with the provider, then saves it in the gateway's vault. */
  async connect(provider: string, apiKey: string): Promise<void> {
    await this.#json("PUT", `/connections/${enc(provider)}`, { api_key: apiKey });
  }

  disconnect(provider: string): Promise<Disconnected> {
    return this.#json("DELETE", `/connections/${enc(provider)}`) as Promise<Disconnected>;
  }

  /** Start Sign in with ChatGPT; `newAccount` adds another ChatGPT account. */
  startChatGPTSignIn(newAccount = false): Promise<ChatGPTSignInStart> {
    return this.#json("POST", "/connections/openai/chatgpt", {
      new_account: newAccount,
    }) as Promise<ChatGPTSignInStart>;
  }

  chatGPTSignIn(loginId: string): Promise<ChatGPTSignInStatus> {
    return this.#json(
      "GET",
      `/connections/openai/chatgpt/${enc(loginId)}`,
    ) as Promise<ChatGPTSignInStatus>;
  }

  checkConnection(provider: string): Promise<ConnectionCheck> {
    return this.#json("POST", `/connections/${enc(provider)}/check`) as Promise<ConnectionCheck>;
  }

  send(
    id: string,
    message: string,
    context: TurnContext = {},
    signal?: AbortSignal,
  ): AsyncGenerator<AgentEvent> {
    return this.#stream(`/conversations/${enc(id)}/turns`, { message, ...context }, signal);
  }

  resume(id: string, signal?: AbortSignal): AsyncGenerator<AgentEvent> {
    return this.#stream(`/conversations/${enc(id)}/resume`, undefined, signal);
  }

  async *#stream(path: string, body: unknown, signal?: AbortSignal): AsyncGenerator<AgentEvent> {
    const res = await this.#request("POST", path, body, signal);
    if (!res.body) throw new GatewayError("stream response has no body", res.status);
    for await (const message of parseSse(res.body)) {
      const event = toAgentEvent(message.event, message.data);
      if (event) yield event;
    }
  }

  async #json(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<unknown> {
    const res = await this.#request(method, path, body, signal);
    return res.status === 204 ? undefined : res.json();
  }

  async #request(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<Response> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["content-type"] = "application/json";
    if (this.#token) headers.authorization = `Bearer ${this.#token}`;
    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    if (signal) init.signal = signal;
    let res: Response;
    try {
      res = await this.#fetch(`${this.#baseUrl}${path}`, init);
    } catch (e: unknown) {
      if (signal?.aborted) throw e; // the caller stopped it: not a connection problem
      throw new GatewayUnreachableError(this.#baseUrl, e);
    }
    if (!res.ok) throw new GatewayError(await describe(res, path), res.status);
    return res;
  }
}

async function describe(res: Response, path: string): Promise<string> {
  let detail = "";
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = `: ${body.detail}`;
    else if (Array.isArray(body.detail)) detail = `: ${body.detail.map(fieldError).join("; ")}`;
  } catch {
    // non-JSON error body: status alone is still a useful message
  }
  return `${path.startsWith("/healthz") ? "healthz" : path} failed: HTTP ${res.status}${detail}`;
}

/** A validation error as "field: reason". The gateway never includes the submitted value. */
function fieldError(e: unknown): string {
  const o = (typeof e === "object" && e !== null ? e : {}) as { loc?: unknown; msg?: unknown };
  const loc = Array.isArray(o.loc) ? o.loc.filter((x) => x !== "body").join(".") : "";
  const msg = typeof o.msg === "string" ? o.msg : "invalid";
  return loc ? `${loc}: ${msg}` : msg;
}

function enc(segment: string): string {
  return encodeURIComponent(segment);
}

function isHealth(v: unknown): v is Health {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return o.status === "ok" && typeof o.version === "string" && typeof o.env === "string";
}
