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
>;

export interface ChatItem {
  role: "user" | "assistant" | "notice";
  text: string;
}

export interface PendingApproval {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
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
};

export type ChatAction =
  | {
      type: "connected";
      conversationId: string;
      brains: Brain[];
      brain: string | null;
      items: ChatItem[];
    }
  | { type: "offline"; error: string }
  | { type: "user_sent"; text: string }
  | { type: "resuming" }
  | { type: "brain_chosen"; brain: string | null }
  | { type: "approval_decided"; id: string }
  | { type: "event"; event: AgentEvent };

const STOP_NOTICES: Record<string, string> = {
  approval_pending: "Waiting for your approval.",
  cancelled: "Stopped.",
};

export function reduce(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "connected":
      return {
        ...state,
        status: "ready",
        error: null,
        conversationId: action.conversationId,
        brains: action.brains,
        brain: action.brain,
        items: action.items,
      };
    case "offline":
      return { ...state, status: "offline", error: action.error, draft: "", working: 0 };
    case "user_sent":
      return {
        ...state,
        status: "streaming",
        error: null,
        items: [...state.items, { role: "user", text: action.text }],
      };
    case "resuming":
      return { ...state, status: "streaming", error: null };
    case "brain_chosen":
      return { ...state, brain: action.brain };
    case "approval_decided":
      return { ...state, approvals: state.approvals.filter((a) => a.id !== action.id) };
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
      const { id, tool } = event;
      return {
        ...state,
        approvals: [...state.approvals, { id, tool, arguments: event.arguments }],
      };
    }
    case "done": {
      const items = [...state.items];
      const notice = STOP_NOTICES[event.stop];
      if (event.text && !notice)
        items.push({
          role: event.stop === "final_answer" ? "assistant" : "notice",
          text: event.text,
        });
      if (notice) items.push({ role: "notice", text: notice });
      return { ...state, status: "ready", items, draft: "", working: 0, lastCost: event.cost_usd };
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

export function formatCost(cost: number | null | undefined): string {
  if (cost === undefined) return "";
  if (cost === null) return "cost unknown";
  return `cost $${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}`;
}
