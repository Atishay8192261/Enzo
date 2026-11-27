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
  /** It can see images (older gateways leave it out: treat as false). */
  vision?: boolean;
}

/** How an image came to Enzo: pasted (or from the clipboard), or a file the user picked. */
export type ImageSource = "pasted" | "file";

/** An image the gateway kept (cleaned of its metadata), to send with a message by `id`. */
export interface UploadedImage {
  id: string;
  media_type: string;
  width: number;
  height: number;
  bytes: number;
  name: string | null;
  /** What was removed before it was kept: "EXIF", "GPS position", ... */
  stripped: string[];
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
  /** Whether Enzo also finds things by meaning, in plain words (ADR 0030); absent from older gateways. */
  search?: string | null;
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

/**
 * A watch (spec §9.4): Enzo checks something on its own with no model call and wakes up only
 * when it changes. It starts off; turning it on needs a check the user saw (`checkWatch`).
 */
export interface Watch {
  id: number;
  /** "the price of the blue kettle". */
  what: string;
  status: "off" | "on" | "stopped" | "done" | "cancelled";
  /** "the text at “.price” on shop.example/kettle, every 30 minutes". */
  checks: string;
  /** What counts as a change: "the number in it goes below 40". */
  change: string;
  /** "until Thu 10 Dec 2026, at most 1 wake". */
  ends: string;
  /** What Enzo does when it changes. */
  on_change: string;
  /** What it read last (outside content: show it as plain text), and when. */
  reads: string | null;
  read_at: string | null;
  next_check: string | null;
  /** Why a stopped one stopped: rate_limited, auth, failing. */
  stop_reason: string | null;
  last_error: string | null;
  wakes: number;
}

/** A watch read right now: what the user sees before turning it on. */
export interface WatchCheck {
  watch: Watch;
  reads: string;
  change: string;
  checked_at: string | null;
  /** Turning it on names this check: the one the user saw. */
  token: string;
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

/** How a task run or a watch's wake went: a report, nothing new yet, done, waiting for
 * approval, failed, or (a watch) stopped. */
export type RunOutcome =
  | "delivered"
  | "reported"
  | "quiet"
  | "finished"
  | "needs_approval"
  | "failed"
  | "stopped";

/** A reminder, a task's report or a watch's, that arrived in a chat: shown after `after`
 * messages. */
export interface ScheduledNotice {
  kind: "reminder" | "task" | "watch";
  /** A task: what it does. A watch: what it watches. */
  task: string | null;
  /** A reminder's words, or the run's report (a watch's is the whole line to show). */
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

/** How a helper is doing: at work, waiting for the user's approval, or how it ended. */
export type HelperStatus = "working" | "waiting" | "done" | "failed" | "stopped" | "interrupted";

/** A helper: long work Enzo handed to a child agent in its own chat (spec §9.5). */
export interface Helper {
  id: number;
  /** A short name: "Compare 5 laptops". */
  title: string;
  status: HelperStatus;
  /** 1: the user's chat handed it the work; 2: another helper did. */
  depth: number;
  /** Its own chat: open it to watch it, answer its approval, or stop it (Esc). */
  chat_id: string | null;
  /** The chat that handed it the work, and the user's own chat at the top. */
  parent_id: string | null;
  conversation_id: string | null;
  coordinator_id: number | null;
  created_at: string;
  finished_at: string | null;
  /** Steps it took, and what its model calls used (the user's own accounts pay). */
  steps: number;
  calls: number;
  cost_usd: number | null;
  plan: boolean;
  /** While it waits: the action it asks the user to approve, in plain words. */
  waiting: string | null;
}

/** A line about a helper in a chat's timeline: "Helper #3 “Compare 5 laptops” finished." */
export interface HelperNotice {
  helper: number;
  kind: "started" | "waiting" | "done" | "failed" | "stopped" | "interrupted" | "resumed";
  text: string;
  after: number;
  at: string;
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
    }
  | {
      kind: "helper";
      /** Null for "reported": the chat that asked replied about its helpers' reports. */
      helper: number | null;
      status: HelperStatus | "reported" | "started" | "resumed";
      /** The line to show, or null when there is nothing to say (a step, a reply). */
      words: string | null;
      /** The helper's own chat, and the user's chat its lines show in. */
      chat_id?: string | null;
      conversation_id: string | null;
    }
  | {
      kind: "watch";
      watch: number;
      outcome: RunOutcome;
      /** The whole line to show: "the kettle's price: now $39.00, under your $40." */
      words: string;
      chat_id: string | null;
      conversation_id: string | null;
      /** The watch is no longer on (its last wake, or it stopped). */
      stopped: boolean;
    };

export interface ApprovalCard {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  summary: string | null;
  detail: string | null;
  /** Private details the action includes, one plain line each (ADR 0029). */
  privacy?: string[];
}

/** A discretion rule the user stated (spec §12.5): what Enzo never shares with others. */
export interface PrivacyRule {
  id: number;
  /** The rule in plain words: "Don't mention my health to anyone". */
  text: string;
  kinds: string[];
  words: string[];
  /** All it covers, in words: "health, and the words “work”". */
  covers: string;
  /** "all": every chat; "chat": only the chat it was set in. */
  scope: "all" | "chat";
  conversation_id: string | null;
  /** That chat's title, when it has one. */
  chat: string | null;
  /** The user's own words that stated it. */
  quote: string;
  created_at: string;
}

export interface StoredMessage {
  role: "system" | "user" | "assistant" | "tool";
  parts: StoredPart[];
  /**
   * Who wrote it, when not the user: "schedule" (a scheduled task's run), "watch" (a watch
   * saw a change), "brief" (the work a helper was given, in its own chat), "handoff"
   * (helpers' reports, for Enzo to review).
   */
  origin?: "schedule" | "watch" | "brief" | "handoff";
  /** Older messages were summarized just before this one (how many turns the summary covers). */
  summarized?: number;
}

/** One part of a stored message: text, an image, a tool call (with its step) or a result. */
export interface StoredPart {
  type: string;
  text?: string;
  /** An image (`type: "image"`): its size; a tool result's images are in `images`. */
  width?: number;
  height?: number;
  images?: StoredPart[];
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

/** What a Google sign-in asks for: everything Enzo offers, or reading only. */
export type GoogleAccess = "full" | "read";

/** A Sign in with Google attempt, or (no OAuth client yet) a request for the user's own. */
export interface GoogleSignInStart {
  login_id: string | null;
  /** A local address on the gateway that forwards to Google's consent page. */
  url: string | null;
  needs_client: boolean;
  message: string | null;
}

export interface GoogleSignInStatus {
  status: "pending" | "connected" | "failed";
  message: string | null;
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
  /** Images kept with `uploadImage` first, in the order the user attached them. */
  images?: string[];
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

  /** Watches that are on, off or stopped, newest first. */
  async watches(): Promise<Watch[]> {
    const body = (await this.#json("GET", "/watches")) as { watches: Watch[] };
    return body.watches;
  }

  /** Read a watch right now (its dry run), to show the user before turning it on. */
  checkWatch(id: number): Promise<WatchCheck> {
    return this.#json("POST", `/watches/${id}/check`) as Promise<WatchCheck>;
  }

  /** Turn a watch on, from the check the user saw. Only after they said yes. */
  enableWatch(id: number, token: string): Promise<Watch> {
    return this.#json("POST", `/watches/${id}/enable`, { token }) as Promise<Watch>;
  }

  async cancelWatch(id: number): Promise<void> {
    await this.#json("DELETE", `/watches/${id}`);
  }

  /** The reminders and task reports that arrived in a chat, in order. */
  conversationScheduled(id: string): Promise<ScheduledNotice[]> {
    return this.#json("GET", `/conversations/${enc(id)}/scheduled`) as Promise<ScheduledNotice[]>;
  }

  /**
   * Live events while the app is open. Ends if the gateway goes away. `opened` is called once
   * the stream is open: what happened while it was not (a restart) can be caught up then.
   */
  async *events(signal?: AbortSignal, opened?: () => void): AsyncGenerator<GatewayEvent> {
    const res = await this.#request("GET", "/events", undefined, signal);
    if (!res.body) throw new GatewayError("event stream has no body", res.status);
    opened?.();
    for await (const message of parseSse(res.body)) {
      if (!["reminder", "task", "helper", "watch"].includes(message.event)) continue; // not known here
      try {
        yield { kind: message.event, ...JSON.parse(message.data) } as GatewayEvent;
      } catch {
        // a malformed event is skipped, never fatal
      }
    }
  }

  /** Helpers at work first, then the latest that finished. */
  async helpers(): Promise<Helper[]> {
    const body = (await this.#json("GET", "/helpers")) as { helpers: Helper[] };
    return body.helpers;
  }

  /** Stop a helper (and the helpers it handed parts to). False: it was not working any more. */
  async stopHelper(id: number): Promise<{ stopped: boolean; helper: Helper }> {
    return (await this.#json("POST", `/helpers/${id}/stop`)) as {
      stopped: boolean;
      helper: Helper;
    };
  }

  /** The lines about helpers in a chat's timeline, in order. */
  conversationHelpers(id: string): Promise<HelperNotice[]> {
    return this.#json("GET", `/conversations/${enc(id)}/helpers`) as Promise<HelperNotice[]>;
  }

  /** Everything Enzo remembers, newest first. */
  memories(): Promise<MemoryList> {
    return this.#json("GET", "/memory") as Promise<MemoryList>;
  }

  /** Forgets a memory for good, with its earlier versions. */
  async forgetMemory(id: number): Promise<void> {
    await this.#json("DELETE", `/memory/${id}`);
  }

  /** The discretion rules that hold in a chat (its own and every chat's); with no chat,
   * the ones for every chat. */
  privacyRules(conversationId?: string | null): Promise<PrivacyRule[]> {
    const query = conversationId ? `?conversation=${enc(conversationId)}` : "";
    return this.#json("GET", `/privacy${query}`) as Promise<PrivacyRule[]>;
  }

  /** Lifts a discretion rule. */
  async removePrivacyRule(id: number): Promise<void> {
    await this.#json("DELETE", `/privacy/${id}`);
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

  /** Start Sign in with Google (Gmail, Calendar); `needs_client` asks for the user's own client. */
  startGoogleSignIn(access: GoogleAccess = "full"): Promise<GoogleSignInStart> {
    return this.#json("POST", "/connections/google/sign-in", {
      access,
    }) as Promise<GoogleSignInStart>;
  }

  googleSignIn(loginId: string): Promise<GoogleSignInStatus> {
    return this.#json(
      "GET",
      `/connections/google/sign-in/${enc(loginId)}`,
    ) as Promise<GoogleSignInStatus>;
  }

  /** Keep the user's own Google OAuth client (the downloaded file's text) in the vault. */
  async setGoogleClient(client: string): Promise<void> {
    await this.#json("PUT", "/connections/google/client", { client });
  }

  /**
   * Keep an image for a message: the gateway reads its real type from the bytes, strips its
   * metadata (camera, GPS) and answers with the id to send. The name never goes in the URL.
   */
  async uploadImage(
    data: Blob | ArrayBuffer | Uint8Array,
    { source = "pasted", name, type }: { source?: ImageSource; name?: string; type?: string } = {},
  ): Promise<UploadedImage> {
    const headers: Record<string, string> = {
      "content-type": type || "application/octet-stream",
      "x-enzo-image-source": source,
    };
    if (name) headers["x-enzo-image-name"] = encodeURIComponent(name);
    const res = await this.#request("POST", "/images", data, undefined, headers);
    return (await res.json()) as UploadedImage;
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
    /** Set: `body` is sent as it is (raw bytes), with these headers. */
    raw?: Record<string, string>,
  ): Promise<Response> {
    const headers: Record<string, string> = { ...raw };
    if (body !== undefined && !raw) headers["content-type"] = "application/json";
    if (this.#token) headers.authorization = `Bearer ${this.#token}`;
    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = raw ? (body as BodyInit) : JSON.stringify(body);
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
