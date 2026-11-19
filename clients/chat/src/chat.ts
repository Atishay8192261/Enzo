import type { AgentEvent, Brain, GatewayClient, StoredMessage } from "@enzo/sdk";

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
>;

export interface ChatItem {
  role: "user" | "assistant" | "notice";
  text: string;
  /** The brain that wrote this reply (live replies; stored history does not record it). */
  by?: string;
  /** For a notice about a failed turn: why, so each surface can say what to do. */
  failure?: string;
  /** A message of the user's that the gateway did not accept (it never reached Enzo). */
  unsent?: true;
  /** A notice that should offer to stop the running reply (Esc in the terminal, a button on web). */
  offer?: "stop";
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
  brains: Brain[];
  brain: string | null;
  items: ChatItem[];
  draft: string; // the assistant reply as it streams in
  working: number; // tools running right now
  approvals: PendingApproval[];
  lastCost: number | null | undefined; // undefined: no turn yet; null: unknown
  /** The last reply was paid by the user's ChatGPT plan (shown instead of a price). */
  lastPlan: boolean;
}

export const initialState: ChatState = {
  status: "connecting",
  error: null,
  conversationId: null,
  brains: [],
  brain: null,
  items: [],
  draft: "",
  working: 0,
  approvals: [],
  lastCost: undefined,
  lastPlan: false,
};

export type ChatAction =
  | {
      type: "connected";
      conversationId: string;
      brains: Brain[];
      brain: string | null;
      items: ChatItem[];
      /** Cards still open in this chat (a reopened app shows them, #39). */
      approvals?: PendingApproval[];
      /** A reply is still being written by an earlier session. */
      running?: boolean;
    }
  | { type: "offline"; error: string }
  | { type: "unsent"; reason: "busy" | "failed"; message?: string }
  | { type: "stream_lost" }
  | { type: "user_sent"; text: string }
  | { type: "resuming" }
  | { type: "brain_chosen"; brain: string | null }
  | { type: "brains_loaded"; brains: Brain[] }
  | { type: "approval_decided"; id: string; approved: boolean }
  | { type: "event"; event: AgentEvent };

/** Said when a message is refused because a reply is still being written (HTTP 409). */
export const BUSY = "Not sent: Enzo is still finishing the last reply.";
export const STILL_RUNNING = "Enzo is still finishing a reply in this chat.";
export const LOST =
  "Lost the connection to Enzo mid-reply. What arrived is kept above; send again to continue.";

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
        brains: action.brains,
        brain: action.brain,
        items: action.running
          ? [...action.items, { role: "notice", text: STILL_RUNNING, offer: "stop" }]
          : action.items,
        draft: "",
        working: 0,
        approvals: action.approvals ?? [],
        lastCost: undefined,
        lastPlan: false,
      };
    case "offline":
      return { ...state, status: "offline", error: action.error, draft: "", working: 0 };
    case "user_sent": {
      // Sending a message instead of answering a card means "not this": the gateway closes it
      // as not run (#39), and the chat says so.
      const skipped: ChatItem[] = state.approvals.map((a) => ({
        role: "notice",
        text: `Not approved: ${describeApproval(a)} (you sent a new message)`,
      }));
      return {
        ...state,
        status: "streaming",
        error: null,
        approvals: [],
        items: [...state.items, ...skipped, { role: "user", text: action.text }],
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
          : `Not sent: ${action.message ?? "the gateway did not answer"}.`;
      items.push(
        action.reason === "busy"
          ? { role: "notice", text: why, offer: "stop" }
          : { role: "notice", text: why },
      );
      return { ...state, status: "ready", draft: "", working: 0, items };
    }
    case "stream_lost": {
      const items = keepDraft(state, [...state.items]);
      items.push({ role: "notice", text: LOST });
      return { ...state, status: "ready", draft: "", working: 0, items };
    }
    case "resuming":
      return { ...state, status: "streaming", error: null };
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
      return onEvent(state, action.event);
  }
}

function onEvent(state: ChatState, event: AgentEvent): ChatState {
  switch (event.type) {
    case "text":
      // Chunks carry their own spacing (even mid-word): join them exactly as they arrive.
      return { ...state, draft: state.draft + event.text };
    case "tool_started": {
      // The next text comes from a new step of the turn: start it on its own line.
      const draft = state.draft && !state.draft.endsWith("\n") ? `${state.draft}\n` : state.draft;
      return { ...state, draft, working: state.working + 1 };
    }
    case "tool_finished":
      return { ...state, working: Math.max(0, state.working - 1) };
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
        ? [...state.items]
        : keepDraft(state, [...state.items], event.brain, event.text);
      if (event.text && !notice) {
        items.push({
          role: answer ? "assistant" : "notice",
          text: event.text,
          ...(answer && event.brain ? { by: event.brain } : {}),
          ...(event.failure ? { failure: event.failure } : {}),
        });
      }
      if (notice) items.push({ role: "notice", text: notice });
      return {
        ...state,
        status: "ready",
        items,
        draft: "",
        working: 0,
        lastCost: event.cost_usd,
        lastPlan: event.plan,
      };
    }
    case "error":
      return { ...state, status: "ready", draft: "", working: 0, error: event.message };
  }
}

/** Stored transcript → chat bubbles. Tool traffic stays out of the chat (spec §10.3). */
export function toItems(messages: StoredMessage[]): ChatItem[] {
  return messages.flatMap((m): ChatItem[] => {
    if (m.role !== "user" && m.role !== "assistant") return [];
    const texts = m.parts.filter((p) => p.type === "text" && p.text).map((p) => p.text as string);
    // A user message's second text part is delivery context (time, device), not words.
    const text = m.role === "user" ? (texts[0] ?? "") : texts.join("");
    return text ? [{ role: m.role, text }] : [];
  });
}

/** The last reply's cost. `plan`: the ChatGPT plan paid, so there is no per-token price. */
export function formatCost(cost: number | null | undefined, plan = false): string {
  if (cost === undefined) return "";
  if (plan) return cost === 0 ? "Using ChatGPT plan" : `${formatCost(cost)} + ChatGPT plan`;
  if (cost === null) return "cost unknown";
  if (cost === 0) return "no charge";
  return `cost $${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}`;
}
