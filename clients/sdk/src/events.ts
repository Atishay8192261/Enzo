/** Events a turn streams from the gateway. Unknown events are skipped; malformed ones throw. */

export type AgentEvent =
  | { type: "text"; text: string }
  | { type: "tool_started"; id: string; tool: string }
  | { type: "tool_finished"; id: string; tool: string; status: string; latency_ms: number }
  | { type: "approval_needed"; id: string; tool: string; arguments: Record<string, unknown> }
  | {
      type: "done";
      stop: StopReason;
      text: string;
      steps: number;
      cost_usd: number | null;
      /** The brain and served model that answered (spec §1.3.6); null when none did. */
      brain: string | null;
      model: string | null;
    }
  | { type: "error"; message: string };

export type StopReason =
  | "final_answer"
  | "approval_pending"
  | "max_steps"
  | "refusal"
  | "truncated"
  | "brain_failure"
  | "over_budget"
  | "cancelled";

type Shape = Record<string, "string" | "number" | "object" | "number|null" | "string|null">;

const SHAPES: Record<AgentEvent["type"], Shape> = {
  text: { text: "string" },
  tool_started: { id: "string", tool: "string" },
  tool_finished: { id: "string", tool: "string", status: "string", latency_ms: "number" },
  approval_needed: { id: "string", tool: "string", arguments: "object" },
  done: {
    stop: "string",
    text: "string",
    steps: "number",
    cost_usd: "number|null",
    brain: "string|null",
    model: "string|null",
  },
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
