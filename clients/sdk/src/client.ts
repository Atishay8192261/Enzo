import { type AgentEvent, toAgentEvent } from "./events.ts";
import { parseSse } from "./sse.ts";

export interface Health {
  status: "ok";
  version: string;
  env: string;
}

export interface Brain {
  name: string;
  provider: string;
  model: string;
  priced: boolean;
}

export interface Conversation {
  id: string;
  kind: "main" | "side" | "external";
  title: string | null;
  brain: string | null;
}

export interface StoredMessage {
  role: "system" | "user" | "assistant" | "tool";
  parts: Array<{ type: string; text?: string; name?: string; content?: string }>;
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

export interface GatewayClientOptions {
  baseUrl: string;
  token?: string;
  fetch?: typeof fetch;
}

export class GatewayClient {
  readonly #baseUrl: string;
  readonly #token: string | undefined;
  readonly #fetch: typeof fetch;

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

  async chooseBrain(id: string, brain: string | null): Promise<void> {
    await this.#json("PUT", `/conversations/${enc(id)}/brain`, { brain });
  }

  messages(id: string): Promise<StoredMessage[]> {
    return this.#json("GET", `/conversations/${enc(id)}/messages`) as Promise<StoredMessage[]>;
  }

  async decide(id: string, approvalId: string, approved: boolean): Promise<void> {
    await this.#json("POST", `/conversations/${enc(id)}/approvals/${enc(approvalId)}`, {
      approved,
    });
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
    const res = await this.#fetch(`${this.#baseUrl}${path}`, init);
    if (!res.ok) throw new GatewayError(await describe(res, path), res.status);
    return res;
  }
}

async function describe(res: Response, path: string): Promise<string> {
  let detail = "";
  try {
    const body = (await res.json()) as { detail?: unknown };
    detail = typeof body.detail === "string" ? `: ${body.detail}` : "";
  } catch {
    // non-JSON error body: status alone is still a useful message
  }
  return `${path.startsWith("/healthz") ? "healthz" : path} failed: HTTP ${res.status}${detail}`;
}

function enc(segment: string): string {
  return encodeURIComponent(segment);
}

function isHealth(v: unknown): v is Health {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return o.status === "ok" && typeof o.version === "string" && typeof o.env === "string";
}
