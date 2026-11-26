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

/** One past chat in the list (#38). */
export interface ChatSummary {
  id: string;
  /** The first line of its first message, or the user's own name for it. */
  title: string | null;
  brain: string | null;
  created_at: string;
  last_active: string;
  /** How many messages the user sent in it. */
  messages: number;
}

/** One thing Enzo remembers about the user, with where it came from (spec §6.2). */
export interface Remembered {
  id: number;
  text: string;
  kind: "about" | "preference" | "person" | "decision" | "fact";
  /** Who or what it is about, when not the user: a person's name. */
  about: string | null;
  said_by: "user" | "inferred";
  happened_on: string | null;
  learned_at: string;
  /** The chat it was learned in, when that chat still exists. */
  chat: string | null;
  conversation_id: string | null;
  /** All of it in words: "you said, 20 Nov 2026, in “Trip to NYC”". */
  source: string;
}

export interface MemoryList {
  memories: Remembered[];
  /** Where the readable copy is, as the user types it: ~/Enzo/Memory.md. */
  file: string | null;
}

/** A reminder or scheduled task still to come (spec §9.3). */
export interface Scheduled {
  id: number;
  kind: "reminder" | "task";
  /** A reminder's words, or what a task does on each run. */
  text: string;
  next_at: string | null;
  /** The next time in the user's words: "Fri 21 Nov 09:00". */
  next: string | null;
  /** "every weekday at 09:00, until 1 Dec 2026". */
  schedule: string;
  timezone: string;
  repeat: "once" | "hourly" | "daily" | "weekdays" | "weekly" | "monthly";
  ends_on: string | null;
  runs: number;
  /** A task: when it is done (its runs stay quiet until then). */
  stop_when: string | null;
  conversation_id: string | null;
  /** A task's own chat, where its runs happen. */
  chat_id: string | null;
  /** The goal it checks in on: closing the goal cancels it. */
  goal_id?: number | null;
}

/** What a change of a goal rests on (spec §6.5). */
export interface GoalEvidence {
  evidence: string | null;
  /** The user confirmed it, a check verified it, or its date passed. */
  basis: "user_confirmed" | "verified" | "timeline_ended" | null;
  /** Where the evidence can be found again. */
  source: string | null;
  happened_on: string | null;
  recorded_at: string;
  /** How it is known and when, in words: "you confirmed, 24 Nov 2026". */
  said: string;
}

export interface GoalStep {
  number: number;
  text: string;
  done: boolean;
  done_at: string | null;
  evidence: GoalEvidence | null;
}

/** A reminder or scheduled task that checks in on a goal. */
export interface GoalCheckIn {
  id: number;
  kind: "reminder" | "task";
  text: string;
  /** "every Monday at 09:00". */
  schedule: string;
  /** "Mon 30 Nov 09:00". */
  next: string | null;
}

/** Something the user is working toward (spec §6.4). */
export interface Goal {
  id: number;
  title: string;
  status: "open" | "done" | "dropped";
  due_on: string | null;
  /** "Tue 15 Dec 2026". */
  due: string | null;
  /** Where its work lives, as the user types it: ~/Enzo/passport. */
  folder: string | null;
  created_at: string;
  closed_at: string | null;
  /** The chat it was set in, when that chat still exists. */
  chat: string | null;
  conversation_id: string | null;
  steps: GoalStep[];
  check_ins: GoalCheckIn[];
  /** Why it was closed, when it is. */
  closing: GoalEvidence | null;
}

export interface GoalList {
  goals: Goal[];
  /** Where the read-only copy is, as the user types it: ~/Enzo/Goals.md. */
  file: string | null;
}

/** How a task run went: a report, nothing new yet, done, waiting for approval, or failed. */
export type RunOutcome =
  | "delivered"
  | "reported"
  | "quiet"
  | "finished"
  | "needs_approval"
  | "failed";

/** A reminder, or a task's report, that arrived in a chat: shown after `after` messages. */
export interface ScheduledNotice {
  kind: "reminder" | "task";
  /** A task: what it does. */
  task: string | null;
  /** A reminder's words, or the run's report. */
  text: string;
  due_at: string;
  delivered_at: string;
  /** It arrived well after its time: Enzo was not running then. */
  late: boolean;
  after: number;
  outcome: RunOutcome;
  /** When it was due, in the user's zone: "Fri 09:00". */
  due: string;
  /** A task's own chat, where the whole run can be read. */
  chat_id: string | null;
}

/** Something that happened while no turn of this app was running. */
export type GatewayEvent =
  | {
      kind: "reminder";
      reminder: number;
      text: string;
      /** What to show: the text, with a note when it is late. */
      words: string;
      late: boolean;
      conversation_id: string | null;
      /** When a repeating reminder comes next, or null when it is done. */
      next: string | null;
    }
  | {
      kind: "task";
      task: number;
      outcome: RunOutcome;
      /** "Check the weather: Sunny, 21°C." */
      words: string;
      chat_id: string | null;
      conversation_id: string | null;
      /** The task ended with this run. */
      stopped: boolean;
    };

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
  /** "schedule": a scheduled task's run started this turn; the user did not type it. */
  origin?: "schedule";
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

  /** A new chat; `brain` is a model or tier chosen before its first message. */
  createConversation(
    kind: Conversation["kind"] = "main",
    title?: string,
    brain?: string | null,
  ): Promise<Conversation> {
    return this.#json("POST", "/conversations", {
      kind,
      ...(title ? { title } : {}),
      ...(brain ? { brain } : {}),
    }) as Promise<Conversation>;
  }

  /** Past chats with at least one message, most recently active first (#38). */
  conversations(limit = 30): Promise<ChatSummary[]> {
    return this.#json("GET", `/conversations?limit=${limit}`) as Promise<ChatSummary[]>;
  }

  async renameConversation(id: string, title: string): Promise<void> {
    await this.#json("PATCH", `/conversations/${enc(id)}`, { title });
  }

  /** Moves a chat to the trash: out of the list, never destroyed. */
  async deleteConversation(id: string): Promise<void> {
    await this.#json("DELETE", `/conversations/${enc(id)}`);
  }

  /** Reminders and scheduled tasks still to come, soonest first. */
  async scheduled(): Promise<Scheduled[]> {
    const body = (await this.#json("GET", "/scheduled")) as { scheduled: Scheduled[] };
    return body.scheduled;
  }

  async cancelScheduled(id: number): Promise<void> {
    await this.#json("DELETE", `/scheduled/${id}`);
  }

  /** The reminders and task reports that arrived in a chat, in order. */
  conversationScheduled(id: string): Promise<ScheduledNotice[]> {
    return this.#json("GET", `/conversations/${enc(id)}/scheduled`) as Promise<ScheduledNotice[]>;
  }

  /** Live events while the app is open. Ends if the gateway goes away. */
  async *events(signal?: AbortSignal): AsyncGenerator<GatewayEvent> {
    const res = await this.#request("GET", "/events", undefined, signal);
    if (!res.body) throw new GatewayError("event stream has no body", res.status);
    for await (const message of parseSse(res.body)) {
      if (message.event !== "reminder" && message.event !== "task") continue; // not known here
      try {
        yield { kind: message.event, ...JSON.parse(message.data) } as GatewayEvent;
      } catch {
        // a malformed event is skipped, never fatal
      }
    }
  }

  /** Everything Enzo remembers, newest first. */
  memories(): Promise<MemoryList> {
    return this.#json("GET", "/memory") as Promise<MemoryList>;
  }

  /** Forgets a memory for good, with its earlier versions. */
  async forgetMemory(id: number): Promise<void> {
    await this.#json("DELETE", `/memory/${id}`);
  }

  /** Open goals (due soonest first), or closed ones (done and given up, newest first). */
  goals(which: "open" | "closed" = "open"): Promise<GoalList> {
    return this.#json("GET", `/goals?which=${which}`) as Promise<GoalList>;
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
