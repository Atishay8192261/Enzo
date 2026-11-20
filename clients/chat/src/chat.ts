import type {
  AgentEvent,
  Brain,
  ConversationUsage,
  GatewayClient,
  StoredMessage,
  StoredPart,
  Usage,
} from "@enzo/sdk";

/** What the chat UI needs from the gateway. A narrow interface keeps the UI testable. */
export type ChatClient = Pick<
  GatewayClient,
  | "health"
  | "brains"
  | "createConversation"
  | "conversation"
  | "messages"
  | "chooseBrain"
  | "send"
  | "resume"
  | "decide"
  | "stop"
  | "usage"
>;

/** Listing and tidying past chats (#38): not every surface has these yet. */
export type ChatsClient = Pick<
  GatewayClient,
  "conversations" | "renameConversation" | "deleteConversation"
>;

/** How a step went: running now, done, failed, not run (declined, moved on) or stopped. */
export type StepStatus = "running" | "ok" | "failed" | "skipped" | "stopped";

export interface ChatItem {
  /** A step is one thing Enzo did for the reply, in plain words: "Reading plan.md" (#40). */
  role: "user" | "assistant" | "notice" | "step";
  text: string;
  /** For a step: the tool call it was, how it went, and how long it took. */
  callId?: string;
  status?: StepStatus;
  ms?: number;
  /** For a user message: when it was sent (ISO time). */
  at?: string;
  /** For the last item of a turn: what the reply used and how long it took (#49). */
  stats?: ReplyStats;
  /** The brain that wrote this reply (live replies; stored history does not record it). */
  by?: string;
  /** For a notice about a failed turn: why, so each surface can say what to do. */
  failure?: string;
  /** A message of the user's that the gateway did not accept (it never reached Enzo). */
  unsent?: true;
  /**
   * What a notice offers: stop the running reply (Esc in the terminal, a button on web), or
   * send the message that did not go through again once Enzo is reachable (#44).
   */
  offer?: "stop" | "resend";
}

/** The facts behind one reply (#49). */
export interface ReplyStats {
  usage: Usage;
  /** Live replies only: from sending to the first word, and to the end, as the user waited. */
  firstWordMs?: number;
  totalMs?: number;
}

export const NO_USAGE: Usage = {
  input_tokens: 0,
  output_tokens: 0,
  cached_input_tokens: 0,
  calls: 0,
  cost_usd: 0,
  plan: false,
  models: [],
  model_ms: 0,
  started_at: null,
};

/** Two usages together: an unknown cost stays unknown, never counted as zero. */
export function addUsage(a: Usage, b: Usage): Usage {
  return {
    input_tokens: a.input_tokens + b.input_tokens,
    output_tokens: a.output_tokens + b.output_tokens,
    cached_input_tokens: a.cached_input_tokens + b.cached_input_tokens,
    calls: a.calls + b.calls,
    cost_usd: a.cost_usd === null || b.cost_usd === null ? null : a.cost_usd + b.cost_usd,
    plan: a.plan || b.plan,
    models: [...a.models, ...b.models.filter((m) => !a.models.includes(m))],
    model_ms: a.model_ms + b.model_ms,
    started_at: a.started_at ?? b.started_at,
  };
}

export interface PendingApproval {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  summary: string | null;
  detail: string | null;
}

/** What the approval card says: the tool's plain description, never a bare tool name (§10.3). */
export function describeApproval(a: PendingApproval): string {
  return a.summary ?? `Run "${a.tool.replaceAll("_", " ")}"`;
}

export interface ChatState {
  status: "connecting" | "ready" | "streaming" | "offline";
  error: string | null;
  conversationId: string | null;
  /** The chat's name: set by the gateway from its first message, or renamed by the user. */
  title: string | null;
  brains: Brain[];
  brain: string | null;
  items: ChatItem[];
  draft: string; // the assistant reply as it streams in
  working: number; // steps running right now
  /** The gateway answers. False while it can't be reached; clients keep checking (#44). */
  online: boolean;
  approvals: PendingApproval[];
  lastCost: number | null | undefined; // undefined: no turn yet; null: unknown
  /** The last reply was paid by the user's ChatGPT plan (shown instead of a price). */
  lastPlan: boolean;
  /** Everything this chat used: from the gateway when it opens, then each reply added. */
  totals: Usage;
  /** When the running turn started and its first word arrived (ms since 1970), for timing. */
  turnStartedAt: number | null;
  firstWordAt: number | null;
}

export const initialState: ChatState = {
  status: "connecting",
  error: null,
  conversationId: null,
  title: null,
  brains: [],
  brain: null,
  items: [],
  draft: "",
  working: 0,
  online: true,
  approvals: [],
  lastCost: undefined,
  lastPlan: false,
  totals: NO_USAGE,
  turnStartedAt: null,
  firstWordAt: null,
};

export type ChatAction =
  | {
      type: "connected";
      /** Null: a fresh chat, created when its first message is sent. */
      conversationId: string | null;
      title?: string | null;
      brains: Brain[];
      brain: string | null;
      items: ChatItem[];
      /** Cards still open in this chat (a reopened app shows them, #39). */
      approvals?: PendingApproval[];
      /** A reply is still being written by an earlier session. */
      running?: boolean;
      /** What this chat used before it was opened here. */
      totals?: Usage;
    }
  | { type: "offline"; error: string }
  /** A fresh chat was created by its first message. */
  | { type: "created"; conversationId: string }
  | { type: "renamed"; title: string }
  /** The gateway stopped answering mid-session, or answers again. */
  | { type: "gateway"; online: boolean }
  | { type: "unsent"; reason: "busy" | "failed" | "unreachable"; message?: string }
  | { type: "stream_lost" }
  /** `at`: when (ms since 1970), so the chat can say how long replies took. */
  | { type: "user_sent"; text: string; at?: number }
  | { type: "resuming"; at?: number }
  | { type: "brain_chosen"; brain: string | null }
  | { type: "brains_loaded"; brains: Brain[] }
  | { type: "approval_decided"; id: string; approved: boolean }
  | { type: "event"; event: AgentEvent; at?: number };

/** Said when a message is refused because a reply is still being written (HTTP 409). */
export const BUSY = "Not sent: Enzo is still finishing the last reply.";
export const STILL_RUNNING = "Enzo is still finishing a reply in this chat.";
export const LOST =
  "Lost the connection to Enzo mid-reply. What arrived is kept above; send again to continue.";
/** Said when a message could not be sent because the gateway did not answer at all (#44). */
export const UNREACHABLE = "Not sent: Enzo can't be reached right now.";
export const BACK_ONLINE = "Connected to Enzo again.";

/** Steps still running when a turn ends early did not finish. */
function stopSteps(items: ChatItem[]): ChatItem[] {
  return items.map((i) => (i.status === "running" ? { ...i, status: "stopped" } : i));
}

/** The mark before a step line (a terminal animates the running one instead). */
export const STEP_MARK: Record<StepStatus, string> = {
  running: "•",
  ok: "✓",
  failed: "✗",
  skipped: "–",
  stopped: "■",
};

const STEP_NOTE: Record<StepStatus, string> = {
  running: "",
  ok: "",
  failed: " · failed",
  skipped: " · not run",
  stopped: " · stopped",
};

/** A step's line, the same on every surface: "Reading plan.md…", "Editing plan.md · failed". */
export function stepLine(item: ChatItem): string {
  const status = item.status ?? "ok";
  return status === "running" ? `${item.text}…` : `${item.text}${STEP_NOTE[status]}`;
}

/** A finished call's status as the chat says it. */
function stepStatus(status: string): StepStatus {
  if (status === "ok") return "ok";
  return status === "declined" || status === "not_run" ? "skipped" : "failed";
}

/** Words that streamed before a turn ended early are never thrown away (#46). */
function keepDraft(
  state: ChatState,
  items: ChatItem[],
  by?: string | null,
  unless?: string,
): ChatItem[] {
  const draft = state.draft.trim();
  if (draft && draft !== unless?.trim()) {
    items.push({ role: "assistant", text: state.draft, ...(by ? { by } : {}) });
  }
  return items;
}

const STOP_NOTICES: Record<string, string> = {
  approval_pending: "Waiting for your approval.",
  cancelled: "Stopped.",
};

export function reduce(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "connected":
      // Also how a new chat starts: nothing from the previous conversation carries over.
      return {
        ...state,
        status: "ready",
        error: null,
        conversationId: action.conversationId,
        title: action.title ?? null,
        brains: action.brains,
        brain: action.brain,
        items: action.running
          ? [...action.items, { role: "notice", text: STILL_RUNNING, offer: "stop" }]
          : action.items,
        draft: "",
        working: 0,
        online: true,
        approvals: action.approvals ?? [],
        lastCost: undefined,
        lastPlan: false,
        totals: action.totals ?? NO_USAGE,
        turnStartedAt: null,
        firstWordAt: null,
      };
    case "created":
      return { ...state, conversationId: action.conversationId };
    case "renamed":
      return { ...state, title: action.title };
    case "offline":
      return {
        ...state,
        status: "offline",
        online: false,
        error: action.error,
        draft: "",
        working: 0,
      };
    case "gateway": {
      if (action.online === state.online) return state;
      if (!action.online) return { ...state, online: false };
      // Back: if a message did not go through, offer to send it again.
      const unsent = state.items.some((i) => i.unsent);
      const notice: ChatItem = unsent
        ? { role: "notice", text: BACK_ONLINE, offer: "resend" }
        : { role: "notice", text: BACK_ONLINE };
      return { ...state, online: true, items: [...state.items, notice] };
    }
    case "user_sent": {
      // Sending a message instead of answering a card means "not this": the gateway closes it
      // as not run (#39), and the chat says so.
      const skipped: ChatItem[] = state.approvals.map((a) => ({
        role: "notice",
        text: `Not approved: ${describeApproval(a)} (you sent a new message)`,
      }));
      const sent: ChatItem =
        action.at === undefined
          ? { role: "user", text: action.text }
          : { role: "user", text: action.text, at: new Date(action.at).toISOString() };
      return {
        ...state,
        status: "streaming",
        error: null,
        approvals: [],
        items: [...state.items, ...skipped, sent],
        turnStartedAt: action.at ?? null,
        firstWordAt: null,
      };
    }
    case "unsent": {
      // The message never reached Enzo: mark it, and say why in plain words (#46).
      const items = [...state.items];
      const last = items.findLastIndex((i) => i.role === "user");
      if (last >= 0) items[last] = { ...(items[last] as ChatItem), unsent: true };
      const why =
        action.reason === "busy"
          ? BUSY
          : action.reason === "unreachable"
            ? UNREACHABLE
            : `Not sent: ${action.message ?? "the gateway did not answer"}.`;
      items.push(
        action.reason === "busy"
          ? { role: "notice", text: why, offer: "stop" }
          : { role: "notice", text: why },
      );
      return {
        ...state,
        status: "ready",
        draft: "",
        working: 0,
        items,
        online: action.reason === "unreachable" ? false : state.online,
      };
    }
    case "stream_lost": {
      const items = keepDraft(state, stopSteps(state.items));
      items.push({ role: "notice", text: LOST });
      return { ...state, status: "ready", draft: "", working: 0, items };
    }
    case "resuming":
      return {
        ...state,
        status: "streaming",
        error: null,
        turnStartedAt: action.at ?? null,
        firstWordAt: null,
      };
    case "brain_chosen":
      return { ...state, brain: action.brain };
    case "brains_loaded":
      return { ...state, brains: action.brains };
    case "approval_decided": {
      // The card goes; a line in the chat keeps what the user decided (spec §15.4).
      const decided = state.approvals.find((a) => a.id === action.id);
      const items: ChatItem[] = decided
        ? [
            ...state.items,
            {
              role: "notice",
              text: `${action.approved ? "Approved" : "Declined"}: ${describeApproval(decided)}`,
            },
          ]
        : state.items;
      return { ...state, items, approvals: state.approvals.filter((a) => a.id !== action.id) };
    }
    case "event":
      return onEvent(state, action.event, action.at);
  }
}

function onEvent(state: ChatState, event: AgentEvent, at?: number): ChatState {
  switch (event.type) {
    case "text":
      // Chunks carry their own spacing (even mid-word): join them exactly as they arrive.
      return {
        ...state,
        draft: state.draft + event.text,
        firstWordAt: state.firstWordAt ?? at ?? null,
      };
    case "tool_started": {
      // Words written before this step stay above it, in the order they happened.
      const items = keepDraft(state, [...state.items]);
      items.push({ role: "step", text: event.label, callId: event.id, status: "running" });
      return { ...state, items, draft: "", working: state.working + 1 };
    }
    case "tool_finished": {
      const status = stepStatus(event.status);
      const index = state.items.findIndex((i) => i.role === "step" && i.callId === event.id);
      if (index < 0) {
        // A call that never started: declined (the chat already says so) or not checkable.
        if (event.status === "declined") return state;
        const step: ChatItem = { role: "step", text: event.label, callId: event.id, status };
        return { ...state, items: [...state.items, step] };
      }
      const items = [...state.items];
      items[index] = { ...(items[index] as ChatItem), status, ms: event.latency_ms };
      return { ...state, items, working: Math.max(0, state.working - 1) };
    }
    case "approval_needed": {
      const { id, tool, summary, detail } = event;
      return {
        ...state,
        approvals: [...state.approvals, { id, tool, arguments: event.arguments, summary, detail }],
      };
    }
    case "done": {
      const notice = STOP_NOTICES[event.stop];
      const answer = event.stop === "final_answer";
      // Words that streamed before the turn stopped early stay, before the reason it stopped.
      const items = answer
        ? stopSteps(state.items)
        : keepDraft(state, stopSteps(state.items), event.brain, event.text);
      if (event.text && !notice) {
        items.push({
          role: answer ? "assistant" : "notice",
          text: event.text,
          ...(answer && event.brain ? { by: event.brain } : {}),
          ...(event.failure ? { failure: event.failure } : {}),
        });
      }
      if (notice) items.push({ role: "notice", text: notice });
      const last = items.at(-1);
      if (last && last.role !== "user")
        items[items.length - 1] = { ...last, stats: statsOf(state, event, at) };
      return {
        ...state,
        status: "ready",
        items,
        draft: "",
        working: 0,
        lastCost: event.cost_usd,
        lastPlan: event.plan,
        totals: addUsage(state.totals, event.usage),
        turnStartedAt: null,
        firstWordAt: null,
      };
    }
    case "error":
      return {
        ...state,
        status: "ready",
        items: stopSteps(state.items),
        draft: "",
        working: 0,
        error: event.message,
      };
  }
}

function statsOf(state: ChatState, event: { usage: Usage }, at?: number): ReplyStats {
  const started = state.turnStartedAt;
  if (started === null || at === undefined) return { usage: event.usage };
  const stats: ReplyStats = { usage: event.usage, totalMs: at - started };
  if (state.firstWordAt !== null) stats.firstWordMs = state.firstWordAt - started;
  return stats;
}

/** Said for a stored step the gateway could not put into words. */
export const SOME_STEP = "Working on a step";

/**
 * Stored transcript → the chat as it looked live: words, and each step Enzo took in plain words.
 * Tool names, arguments and results stay out of the chat (spec §10.3).
 */
export function toItems(messages: StoredMessage[], usage?: ConversationUsage | null): ChatItem[] {
  const results = new Map<string, StepStatus>();
  for (const m of messages) {
    for (const p of m.parts) {
      if (p.type === "tool_result" && p.call_id) results.set(p.call_id, storedStatus(p));
    }
  }
  const items: ChatItem[] = [];
  // Where each turn starts in `items`: a turn is a user message and everything until the next.
  const starts: number[] = [];
  for (const m of messages) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    const texts = m.parts.filter((p) => p.type === "text" && p.text).map((p) => p.text as string);
    if (m.role === "user") {
      // A user message's second text part is delivery context (time, device), not words.
      const text = texts[0] ?? "";
      const sent = usage?.turns[starts.length]?.started_at;
      starts.push(items.length);
      if (text) items.push(sent ? { role: "user", text, at: sent } : { role: "user", text });
      continue;
    }
    const text = texts.join("");
    if (text) items.push({ role: "assistant", text });
    for (const p of m.parts) {
      // A call with no result yet is waiting for approval: its card shows it, not a step.
      const status = p.type === "tool_call" && p.id ? results.get(p.id) : undefined;
      if (status && p.id) {
        const step: ChatItem = { role: "step", text: p.label ?? SOME_STEP, callId: p.id, status };
        if (p.latency_ms !== undefined) step.ms = p.latency_ms;
        items.push(step);
      }
    }
  }
  // Each turn's facts go on its last reply, as they did live (#49).
  starts.forEach((start, turn) => {
    const used = usage?.turns[turn];
    const end = starts[turn + 1] ?? items.length;
    const at = items.slice(start, end).findLastIndex((i) => i.role === "assistant");
    if (used && at >= 0) {
      const item = items[start + at] as ChatItem;
      items[start + at] = { ...item, stats: { usage: used } };
    }
  });
  return items;
}

function storedStatus(result: StoredPart): StepStatus {
  if (!result.is_error) return "ok";
  try {
    const kind = (JSON.parse(result.content ?? "") as { error?: unknown }).error;
    return typeof kind === "string" ? stepStatus(kind) : "failed";
  } catch {
    return "failed";
  }
}

/** The last reply's cost. `plan`: the ChatGPT plan paid, so there is no per-token price. */
export function formatCost(cost: number | null | undefined, plan = false): string {
  if (cost === undefined) return "";
  if (plan) return cost === 0 ? "Using ChatGPT plan" : `${formatCost(cost)} + ChatGPT plan`;
  if (cost === null) return "cost unknown";
  if (cost === 0) return "no charge";
  return `cost $${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}`;
}
