/** Events a turn streams from the gateway. Unknown events are skipped; malformed ones throw. */

export type AgentEvent =
  | { type: "text"; text: string }
  /** `label` is the step in plain words ("Reading plan.md"); never show `tool` to people. */
  | { type: "tool_started"; id: string; tool: string; label: string }
  | {
      type: "tool_finished";
      id: string;
      tool: string;
      label: string;
      /** "ok", or why not: "declined", "not_run", "failed", "timeout", ... */
      status: string;
      latency_ms: number;
    }
  | {
      type: "approval_needed";
      id: string;
      tool: string;
      arguments: Record<string, unknown>;
      /** The exact action in plain words, for the approval card; null when the tool has none. */
      summary: string | null;
      /** What the action will change (e.g. the diff a merge lands); null when there is nothing. */
      detail: string | null;
    }
  | {
      type: "done";
      stop: StopReason;
      text: string;
      steps: number;
      cost_usd: number | null;
      /** The brain and served model that answered (spec §1.3.6); null when none did. */
      brain: string | null;
      model: string | null;
      /** Why a brain_failure happened, e.g. "not_connected", so each client can word it. */
      failure: string | null;
      /** The user's ChatGPT plan paid for (part of) this reply, so there is no per-token cost. */
      plan: boolean;
      /** Tokens, models and time behind the reply (#49). */
      usage: Usage;
    }
  /**
   * Older messages were summarized before this reply, to keep the chat within the model's limits
   * (spec §5.2), or (`ok: false`) summarizing failed and the reply read the whole chat.
   * `text` says so in plain words; `turns` is how many earlier turns the summary covers.
   */
  | { type: "compacted"; text: string; turns: number; ok: boolean }
  | { type: "error"; message: string };

/** What brain calls used: for one reply, one turn, or a whole chat (#49). */
export interface Usage {
  input_tokens: number;
  output_tokens: number;
  /** Input read from the provider's cache (cheaper); part of `input_tokens`. */
  cached_input_tokens: number;
  calls: number;
  /** Per-token cost; null when any call has no published price (never shown as 0). */
  cost_usd: number | null;
  /** The user's ChatGPT plan paid for some of it: there is no per-token price for that. */
  plan: boolean;
  /** The models that answered, in order. */
  models: string[];
  /** Time spent waiting for the models (tools and approvals not included). */
  model_ms: number;
  /** When the first call started (ISO time): about when the message was sent. */
  started_at: string | null;
}

export type StopReason =
  | "final_answer"
  | "approval_pending"
  | "max_steps"
  | "refusal"
  | "truncated"
  | "brain_failure"
  | "over_budget"
  | "cancelled";

type Shape = Record<
  string,
  "string" | "number" | "boolean" | "object" | "number|null" | "string|null"
>;

const SHAPES: Record<AgentEvent["type"], Shape> = {
  text: { text: "string" },
  tool_started: { id: "string", tool: "string", label: "string" },
  tool_finished: {
    id: "string",
    tool: "string",
    label: "string",
    status: "string",
    latency_ms: "number",
  },
  approval_needed: {
    id: "string",
    tool: "string",
    arguments: "object",
    summary: "string|null",
    detail: "string|null",
  },
  done: {
    stop: "string",
    text: "string",
    steps: "number",
    cost_usd: "number|null",
    brain: "string|null",
    model: "string|null",
    failure: "string|null",
    plan: "boolean",
    usage: "object",
  },
  compacted: { text: "string", turns: "number", ok: "boolean" },
  error: { message: "string" },
};

export class MalformedEventError extends Error {
  constructor(event: string, data: string) {
    super(`malformed ${event} event: ${data.slice(0, 200)}`);
    this.name = "MalformedEventError";
  }
}

export function toAgentEvent(event: string, data: string): AgentEvent | undefined {
  if (!Object.hasOwn(SHAPES, event)) return undefined;
  const type = event as AgentEvent["type"];
  let body: unknown;
  try {
    body = JSON.parse(data);
  } catch {
    throw new MalformedEventError(event, data);
  }
  if (!matches(body, SHAPES[type])) throw new MalformedEventError(event, data);
  return { type, ...body } as AgentEvent;
}

function matches(body: unknown, shape: Shape): body is Record<string, unknown> {
  if (typeof body !== "object" || body === null) return false;
  const o = body as Record<string, unknown>;
  return Object.entries(shape).every(([key, kind]) => {
    const v = o[key];
    if (kind === "number|null") return v === null || typeof v === "number";
    if (kind === "string|null") return v === null || typeof v === "string";
    if (kind === "object") return typeof v === "object" && v !== null && !Array.isArray(v);
    return typeof v === kind;
  });
}
