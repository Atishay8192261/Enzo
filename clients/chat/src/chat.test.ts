import type { AgentEvent, Usage } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import {
  BACK_ONLINE,
  BUSY,
  browserSign,
  type ChatItem,
  type ChatState,
  describeApproval,
  formatCost,
  initialState,
  LOST,
  NO_USAGE,
  reduce,
  SOME_STEP,
  STILL_RUNNING,
  stepLine,
  toItems,
  UNREACHABLE,
} from "./chat.ts";

const ready = reduce(initialState, {
  type: "connected",
  conversationId: "c1",
  brains: [],
  brain: null,
  items: [],
});

/** Items without their stats, for tests about the words (stats have their own tests). */
function plain(items: ChatItem[]): ChatItem[] {
  return items.map(({ stats: _stats, ...rest }) => rest);
}

function started(id: string, label: string, tool = "some_tool"): AgentEvent {
  return { type: "tool_started", id, tool, label };
}

function finished(id: string, label: string, status: string, tool = "some_tool"): AgentEvent {
  return { type: "tool_finished", id, tool, label, status, latency_ms: 5 };
}

const DONE = {
  type: "done" as const,
  stop: "final_answer" as const,
  text: "ok",
  steps: 1,
  cost_usd: null,
  brain: null,
  model: null,
  failure: null,
  plan: false,
  usage: NO_USAGE,
};

describe("chat reducer", () => {
  it("keeps words written before a step above it, in the order they happened (#40)", () => {
    let s = reduce(ready, { type: "user_sent", text: "hi" });
    s = reduce(s, { type: "event", event: { type: "text", text: "Let me check." } });
    s = reduce(s, { type: "event", event: started("c1", "Searching the web for “x”") });
    s = reduce(s, { type: "event", event: { type: "text", text: "Found it." } });
    expect(plain(s.items)).toEqual([
      { role: "user", text: "hi" },
      { role: "assistant", text: "Let me check." },
      { role: "step", text: "Searching the web for “x”", callId: "c1", status: "running" },
    ]);
    expect(s.draft).toBe("Found it.");
  });

  it("streams text into a draft, then commits the final answer", () => {
    let s = reduce(ready, { type: "user_sent", text: "hi" });
    // Providers stream raw chunks that carry their own spacing, even mid-word.
    for (const text of ["Hel", "lo", " there"]) {
      s = reduce(s, { type: "event", event: { type: "text", text } });
    }
    expect(s.draft).toBe("Hello there");
    s = reduce(s, {
      type: "event",
      event: {
        type: "done",
        stop: "final_answer",
        text: "hello there",
        steps: 1,
        cost_usd: 0.002,
        brain: "claude-sonnet",
        model: "claude-sonnet-5-5",
        failure: null,
        plan: false,
        usage: NO_USAGE,
      },
    });
    expect(plain(s.items)).toEqual([
      { role: "user", text: "hi" },
      { role: "assistant", text: "hello there", by: "claude-sonnet" },
    ]);
    expect([s.draft, s.lastCost, s.lastPlan]).toEqual(["", 0.002, false]);
    expect(s.status).toBe("ready");
    const done = {
      type: "done" as const,
      stop: "final_answer" as const,
      text: "on the plan",
      steps: 1,
      cost_usd: 0,
      brain: "gpt-sol",
      model: "gpt-6.2-sol",
      failure: null,
      plan: true,
      usage: NO_USAGE,
    };
    s = reduce(reduce(s, { type: "user_sent", text: "again" }), { type: "event", event: done });
    expect([s.lastCost, s.lastPlan]).toEqual([0, true]);
  });

  it("keeps Enzo's words written before an approval is needed", () => {
    let s = reduce(initialState, { type: "user_sent", text: "delete old notes" });
    s = reduce(s, { type: "event", event: { type: "text", text: "I'll tidy that up." } });
    s = reduce(s, {
      type: "event",
      event: {
        type: "done",
        stop: "approval_pending",
        text: "Waiting for your approval: delete_file.",
        steps: 1,
        cost_usd: 0,
        brain: "gpt-sol",
        model: "gpt-6.2-sol",
        failure: null,
        plan: false,
        usage: NO_USAGE,
      },
    });
    expect(plain(s.items)).toEqual([
      { role: "user", text: "delete old notes" },
      { role: "assistant", text: "I'll tidy that up.", by: "gpt-sol" },
      { role: "notice", text: "Waiting for your approval." },
    ]);
  });

  it("keeps a line saying what the user approved or declined", () => {
    const asked: ChatState = {
      ...initialState,
      status: "ready",
      approvals: [
        { id: "a1", tool: "delete_file", arguments: {}, summary: "Delete plan.md", detail: null },
        { id: "a2", tool: "send_email", arguments: {}, summary: null, detail: null },
      ],
    };
    let s = reduce(asked, { type: "approval_decided", id: "a1", approved: true });
    s = reduce(s, { type: "approval_decided", id: "a2", approved: false });
    expect(plain(s.items)).toEqual([
      { role: "notice", text: "Approved: Delete plan.md" },
      { role: "notice", text: 'Declined: Run "send email"' },
    ]);
    expect(s.approvals).toEqual([]);
  });

  it("a reopened chat shows its open approval and a reply still running (#39, #46)", () => {
    const card = {
      id: "a1",
      tool: "delete_file",
      arguments: {},
      summary: "Delete plan.md",
      detail: null,
    };
    const s = reduce(initialState, {
      type: "connected",
      conversationId: "c1",
      brains: [],
      brain: null,
      items: [],
      approvals: [card],
      running: true,
    });
    expect(s.approvals).toEqual([card]);
    expect(plain(s.items)).toEqual([{ role: "notice", text: STILL_RUNNING, offer: "stop" }]);
  });

  it("sending instead of answering a card says the action was not approved (#39)", () => {
    const open: ChatState = {
      ...initialState,
      status: "ready",
      approvals: [
        { id: "a1", tool: "delete_file", arguments: {}, summary: "Delete plan.md", detail: null },
      ],
    };
    const s = reduce(open, { type: "user_sent", text: "never mind" });
    expect(s.approvals).toEqual([]);
    expect(plain(s.items)).toEqual([
      { role: "notice", text: "Not approved: Delete plan.md (you sent a new message)" },
      { role: "user", text: "never mind" },
    ]);
  });

  it("a message refused while a reply runs is marked not sent, with Stop on offer (#46)", () => {
    let s = reduce(
      { ...initialState, status: "ready" },
      { type: "user_sent", text: "are you back?" },
    );
    s = reduce(s, { type: "unsent", reason: "busy" });
    expect(s.status).toBe("ready");
    expect(plain(s.items)).toEqual([
      { role: "user", text: "are you back?", unsent: true },
      { role: "notice", text: BUSY, offer: "stop" },
    ]);
    s = reduce(reduce(s, { type: "user_sent", text: "hi" }), {
      type: "unsent",
      reason: "failed",
      message: "connection refused",
    });
    expect(plain(s.items.slice(-2))).toEqual([
      { role: "user", text: "hi", unsent: true },
      { role: "notice", text: "Not sent: connection refused." },
    ]);
  });

  it("a stream that ends early stops waiting and keeps what arrived (#46)", () => {
    let s = reduce({ ...initialState, status: "ready" }, { type: "user_sent", text: "a story" });
    s = reduce(s, { type: "event", event: { type: "text", text: "Once upon a time" } });
    s = reduce(s, { type: "stream_lost" });
    expect(s.status).toBe("ready");
    expect(plain(s.items.slice(1))).toEqual([
      { role: "assistant", text: "Once upon a time" },
      { role: "notice", text: LOST },
    ]);
  });

  it("a reply cut off by a failure keeps its words, without repeating them (#46)", () => {
    const base = {
      steps: 1,
      cost_usd: null,
      brain: "gpt-sol",
      model: "m",
      plan: false,
      usage: NO_USAGE,
    };
    let s = reduce(
      { ...initialState, status: "streaming", draft: "The robot picked up a whisk" },
      {
        type: "event",
        event: {
          type: "done",
          stop: "brain_failure",
          text: "The reply was cut off.",
          failure: "connection",
          ...base,
        },
      },
    );
    expect(plain(s.items)).toEqual([
      { role: "assistant", text: "The robot picked up a whisk", by: "gpt-sol" },
      { role: "notice", text: "The reply was cut off.", failure: "connection" },
    ]);
    s = reduce(
      { ...initialState, status: "streaming", draft: "half" },
      {
        type: "event",
        event: { type: "done", stop: "truncated", text: "half", failure: null, ...base },
      },
    );
    expect(plain(s.items)).toEqual([{ role: "notice", text: "half" }]);
  });

  it("a new conversation carries nothing over from the old one", () => {
    const busy: ChatState = {
      ...initialState,
      status: "ready",
      items: [{ role: "user", text: "old" }],
      draft: "half",
      working: 1,
      approvals: [{ id: "a1", tool: "t", arguments: {}, summary: null, detail: null }],
      lastCost: 0.5,
      lastPlan: true,
    };
    const fresh = reduce(busy, {
      type: "connected",
      conversationId: "c2",
      brains: [],
      brain: null,
      items: [],
    });
    expect(fresh).toMatchObject({
      conversationId: "c2",
      items: [],
      draft: "",
      working: 0,
      approvals: [],
      lastCost: undefined,
      lastPlan: false,
    });
  });

  it("shows each step in plain words and how it went, never the tool's name (#40)", () => {
    let s = reduce(ready, { type: "event", event: started("t1", "Reading plan.md", "read_file") });
    s = reduce(s, { type: "event", event: started("t2", "Editing plan.md", "edit_file") });
    expect(s.working).toBe(2);
    s = reduce(s, { type: "event", event: finished("t1", "Reading plan.md", "ok", "read_file") });
    s = reduce(s, {
      type: "event",
      event: finished("t2", "Editing plan.md", "failed", "edit_file"),
    });
    expect(s.working).toBe(0);
    expect(plain(s.items)).toEqual([
      { role: "step", text: "Reading plan.md", callId: "t1", status: "ok", ms: 5 },
      { role: "step", text: "Editing plan.md", callId: "t2", status: "failed", ms: 5 },
    ]);
    expect(JSON.stringify(s.items)).not.toMatch(/read_file|edit_file/);
  });

  it("a step that was not run says so; a declined one is already said by its card", () => {
    let s = reduce(ready, { type: "event", event: started("t1", "Reading a.md") });
    s = reduce(s, { type: "event", event: finished("t1", "Reading a.md", "not_run") });
    // never started: the user declined it (the chat already says "Declined: …") ...
    s = reduce(s, { type: "event", event: finished("t2", "Deleting b.md", "declined") });
    // ... or it could not be checked before asking: that must still show
    s = reduce(s, { type: "event", event: finished("t3", "Merging #12", "failed") });
    expect(s.items.map((i) => [i.text, i.status])).toEqual([
      ["Reading a.md", "skipped"],
      ["Merging #12", "failed"],
    ]);
    expect(s.working).toBe(0);
  });

  it("steps still running when a turn ends early are marked stopped", () => {
    const running = reduce(ready, { type: "event", event: started("t1", "Reading a.md") });
    const cancelled = reduce(running, {
      type: "event",
      event: { ...DONE, stop: "cancelled", text: "Stopped." },
    });
    const answered = reduce(running, { type: "event", event: DONE });
    const lost = reduce(running, { type: "stream_lost" });
    const failed = reduce(running, { type: "event", event: { type: "error", message: "x" } });
    for (const s of [cancelled, answered, lost, failed]) {
      expect(s.items[0]).toMatchObject({ role: "step", status: "stopped" });
    }
  });

  it("a step the user stopped says stopped, not failed (ADR 0027)", () => {
    const running = reduce(ready, { type: "event", event: started("b1", "Opening shop.example") });
    const s = reduce(running, {
      type: "event",
      event: finished("b1", "Opening shop.example", "stopped"),
    });
    expect(s.items[0]).toMatchObject({ role: "step", status: "stopped" });
    expect(stepLine(s.items[0] as ChatItem)).toBe("Opening shop.example · stopped");
  });

  it("shows where Enzo is using the browser until the task ends (ADR 0027)", () => {
    let s = reduce(ready, { type: "user_sent", text: "buy the kettle" });
    s = reduce(s, { type: "event", event: { type: "browser", site: "shop.example" } });
    expect(s.browser).toBe("shop.example");
    expect(browserSign(s.browser as string)).toBe("Enzo is using your browser: shop.example");
    // A step waits for the user's yes: its page stays open, and so does the sign.
    const waiting = reduce(s, { type: "event", event: { ...DONE, stop: "approval_pending" } });
    expect(waiting.browser).toBe("shop.example");
    // The turn ends, the gateway says it closed, a new message ends a waiting task, or it fails.
    expect(reduce(s, { type: "event", event: DONE }).browser).toBeNull();
    expect(reduce(s, { type: "event", event: { type: "browser", site: null } }).browser).toBeNull();
    expect(reduce(waiting, { type: "user_sent", text: "never mind" }).browser).toBeNull();
    const failed = reduce(s, { type: "event", event: { type: "error", message: "x" } });
    expect(failed.browser).toBeNull();
    const elsewhere = reduce(s, {
      type: "connected",
      conversationId: "c2",
      brains: [],
      brain: null,
      items: [],
    });
    expect(elsewhere.browser).toBeNull();
  });

  it("holds approvals until decided and says it is waiting", () => {
    let s = reduce(ready, {
      type: "event",
      event: {
        type: "approval_needed",
        id: "appr_1",
        tool: "send_email",
        summary: null,
        detail: null,
        arguments: { to: "bob" },
      },
    });
    s = reduce(s, {
      type: "event",
      event: {
        type: "done",
        stop: "approval_pending",
        text: "Waiting",
        steps: 1,
        cost_usd: null,
        brain: null,
        model: null,
        failure: null,
        plan: false,
        usage: NO_USAGE,
      },
    });
    expect(s.approvals).toEqual([
      { id: "appr_1", tool: "send_email", arguments: { to: "bob" }, summary: null, detail: null },
    ]);
    expect(plain(s.items).at(-1)).toEqual({ role: "notice", text: "Waiting for your approval." });
    s = reduce(s, { type: "approval_decided", id: "appr_1", approved: true });
    expect(s.approvals).toEqual([]);
  });

  it("shows honest failures as notices, not as Enzo's answer", () => {
    const s = reduce(ready, {
      type: "event",
      event: {
        type: "done",
        stop: "brain_failure",
        text: "I could not get an answer: no API key",
        steps: 0,
        cost_usd: null,
        brain: null,
        model: null,
        failure: null,
        plan: false,
        usage: NO_USAGE,
      },
    });
    expect(plain(s.items)).toEqual([
      { role: "notice", text: "I could not get an answer: no API key" },
    ]);
  });
});

describe("toItems", () => {
  it("keeps words, drops delivery context and tool traffic", () => {
    expect(
      toItems([
        {
          role: "user",
          parts: [
            { type: "text", text: "hi" },
            { type: "text", text: "[sent …; device: web]" },
          ],
        },
        { role: "assistant", parts: [{ type: "native" }, { type: "tool_call", name: "x" }] },
        { role: "tool", parts: [{ type: "tool_result", content: "{}" }] },
        { role: "assistant", parts: [{ type: "text", text: "done" }] },
      ]),
    ).toEqual([
      { role: "user", text: "hi" },
      { role: "assistant", text: "done" },
    ]);
  });

  it("a reopened chat shows when each message was sent and what each reply used (#49)", () => {
    const turn = (input: number, at: string) => ({
      ...NO_USAGE,
      input_tokens: input,
      started_at: at,
    });
    const items = toItems(
      [
        { role: "user", parts: [{ type: "text", text: "one" }] },
        {
          role: "assistant",
          parts: [
            { type: "text", text: "Looking." },
            { type: "tool_call", id: "a", name: "x", label: "Reading a.md", latency_ms: 12 },
          ],
        },
        { role: "tool", parts: [{ type: "tool_result", call_id: "a", content: "{}" }] },
        { role: "assistant", parts: [{ type: "text", text: "first" }] },
        { role: "user", parts: [{ type: "text", text: "two" }] },
        { role: "assistant", parts: [{ type: "text", text: "second" }] },
      ],
      {
        total: { ...NO_USAGE, input_tokens: 30 },
        turns: [turn(10, "2026-11-20T09:00:00Z"), turn(20, "2026-11-20T09:05:00Z")],
      },
    );
    expect(items).toEqual([
      { role: "user", text: "one", at: "2026-11-20T09:00:00Z" },
      { role: "assistant", text: "Looking." }, // the facts go on the turn's last reply only
      { role: "step", text: "Reading a.md", callId: "a", status: "ok", ms: 12 },
      { role: "assistant", text: "first", stats: { usage: turn(10, "2026-11-20T09:00:00Z") } },
      { role: "user", text: "two", at: "2026-11-20T09:05:00Z" },
      { role: "assistant", text: "second", stats: { usage: turn(20, "2026-11-20T09:05:00Z") } },
    ]);
  });

  it("a reopened chat still shows each step Enzo took and how it went (#40)", () => {
    const declined = JSON.stringify({ error: "declined", message: "The user declined" });
    const failed = JSON.stringify({ error: "timeout", message: "too slow" });
    expect(
      toItems([
        { role: "user", parts: [{ type: "text", text: "tidy my notes" }] },
        {
          role: "assistant",
          parts: [
            { type: "text", text: "Looking first." },
            { type: "tool_call", id: "a", name: "read_file", label: "Reading notes.md" },
            { type: "tool_call", id: "b", name: "read_page", label: "Reading bbc.com/news" },
          ],
        },
        {
          role: "tool",
          parts: [
            { type: "tool_result", call_id: "a", name: "read_file", content: "{}" },
            { type: "tool_result", call_id: "b", content: failed, is_error: true },
          ],
        },
        {
          role: "assistant",
          parts: [
            { type: "tool_call", id: "c", name: "delete_file", label: "Deleting old.md" },
            { type: "tool_call", id: "d", name: "x" }, // no words given: still a step
            { type: "tool_call", id: "e", name: "edit_file", label: "Editing notes.md" },
          ],
        },
        {
          role: "tool",
          parts: [
            { type: "tool_result", call_id: "c", content: declined, is_error: true },
            { type: "tool_result", call_id: "d", content: "{}" },
            // "e" has no result yet: it waits for approval, and its card shows it
          ],
        },
      ]),
    ).toEqual([
      { role: "user", text: "tidy my notes" },
      { role: "assistant", text: "Looking first." },
      { role: "step", text: "Reading notes.md", callId: "a", status: "ok" },
      { role: "step", text: "Reading bbc.com/news", callId: "b", status: "failed" },
      { role: "step", text: "Deleting old.md", callId: "c", status: "skipped" },
      { role: "step", text: SOME_STEP, callId: "d", status: "ok" },
    ]);
  });
});

describe("what this chat used (#49)", () => {
  const used = (input: number, cost: number | null, plan = false): Usage => ({
    ...NO_USAGE,
    input_tokens: input,
    output_tokens: 1,
    calls: 1,
    cost_usd: cost,
    plan,
    models: [plan ? "gpt-6.2-sol" : "claude-sonnet-5-5"],
  });
  const turn = (s: ChatState, usage: Usage) =>
    reduce(s, { type: "event", event: { ...DONE, usage }, at: 0 });

  it("adds each reply to what the chat used before, and an unknown cost stays unknown", () => {
    const opened = reduce(initialState, {
      type: "connected",
      conversationId: "c1",
      brains: [],
      brain: null,
      items: [],
      totals: used(1000, 0.5),
    });
    let s = turn(turn(opened, used(10, 0.25)), used(5, 0, true));
    expect(s.totals).toMatchObject({
      input_tokens: 1015,
      output_tokens: 3,
      calls: 3,
      cost_usd: 0.75,
      plan: true,
    });
    expect(s.totals.models).toEqual(["claude-sonnet-5-5", "gpt-6.2-sol"]);
    s = turn(turn(s, used(1, null)), used(1, 1));
    expect(s.totals.cost_usd).toBeNull(); // never a made-up total
    const next = reduce(s, {
      type: "connected",
      conversationId: "c2",
      brains: [],
      brain: null,
      items: [],
    });
    expect(next.totals).toEqual(NO_USAGE);
  });

  it("each live reply says what it used and how long it took", () => {
    let s = reduce(ready, { type: "user_sent", text: "hi", at: 1000 });
    expect(s.items[0]?.at).toBe(new Date(1000).toISOString());
    s = reduce(s, { type: "event", event: { type: "text", text: "Hel" }, at: 1400 });
    s = reduce(s, { type: "event", event: { type: "text", text: "lo" }, at: 1500 });
    s = reduce(s, { type: "event", event: started("t1", "Reading a.md") });
    s = reduce(s, { type: "event", event: finished("t1", "Reading a.md", "ok") });
    s = reduce(s, {
      type: "event",
      event: { ...DONE, text: "Hello", usage: used(7, 0.1) },
      at: 3000,
    });
    const reply = s.items.at(-1);
    expect(reply).toMatchObject({ role: "assistant", text: "Hello" });
    expect(reply?.stats).toEqual({ usage: used(7, 0.1), firstWordMs: 400, totalMs: 2000 });
    expect(s.items.find((i) => i.role === "step")?.ms).toBe(5);
  });

  it("a reply after an approval is timed from the approval, and a stop still says what it used", () => {
    let s = reduce(ready, { type: "resuming", at: 5000 });
    s = reduce(s, { type: "event", event: { ...DONE, usage: used(3, 0) }, at: 5600 });
    expect(s.items.at(-1)?.stats?.totalMs).toBe(600);
    s = reduce(s, { type: "user_sent", text: "go", at: 6000 });
    s = reduce(s, {
      type: "event",
      event: { ...DONE, stop: "cancelled", text: "Stopped.", usage: used(2, 0) },
      at: 6100,
    });
    expect(s.items.at(-1)).toMatchObject({ role: "notice", text: "Stopped." });
    expect(s.items.at(-1)?.stats?.usage.input_tokens).toBe(2);
  });
});

describe("when the gateway can't be reached (#44)", () => {
  it("a message that could not be sent says so plainly and marks Enzo offline", () => {
    let s = reduce(ready, { type: "user_sent", text: "are you there?" });
    s = reduce(s, { type: "unsent", reason: "unreachable" });
    expect(plain(s.items)).toEqual([
      { role: "user", text: "are you there?", unsent: true },
      { role: "notice", text: UNREACHABLE },
    ]);
    expect(UNREACHABLE).not.toMatch(/ECONN|fetch|Unable to connect/);
    expect([s.online, s.status]).toEqual([false, "ready"]);
  });

  it("coming back offers to send the message that did not go through", () => {
    let s = reduce(ready, { type: "user_sent", text: "hi" });
    s = reduce(s, { type: "unsent", reason: "unreachable" });
    s = reduce(s, { type: "gateway", online: true });
    expect(s.online).toBe(true);
    expect(s.items.at(-1)).toEqual({ role: "notice", text: BACK_ONLINE, offer: "resend" });
  });

  it("going offline and back without a lost message only says it is back, once", () => {
    let s = reduce(ready, { type: "gateway", online: false });
    expect([s.online, s.items]).toEqual([false, []]);
    s = reduce(s, { type: "gateway", online: false });
    s = reduce(s, { type: "gateway", online: true });
    s = reduce(s, { type: "gateway", online: true });
    expect(plain(s.items)).toEqual([{ role: "notice", text: BACK_ONLINE }]);
  });
});

describe("formatCost", () => {
  it("never shows an unknown cost as zero", () => {
    expect(formatCost(undefined)).toBe("");
    expect(formatCost(null)).toBe("cost unknown");
    expect(formatCost(0.00123)).toBe("cost $0.0012");
    expect(formatCost(1.5)).toBe("cost $1.50");
  });
});

describe("formatCost", () => {
  it("says no charge when nothing was spent, and never invents a price", () => {
    expect(formatCost(undefined)).toBe("");
    expect(formatCost(null)).toBe("cost unknown");
    expect(formatCost(0)).toBe("no charge");
    expect(formatCost(0, true)).toBe("Using ChatGPT plan");
    expect(formatCost(0.0021, true)).toBe("cost $0.0021 + ChatGPT plan");
    expect(formatCost(0.0021)).toBe("cost $0.0021");
  });
});

describe("describeApproval", () => {
  it("uses the tool's plain words, and never shows a bare tool name", () => {
    const base = { id: "a", tool: "delete_file", arguments: { path: "x" }, detail: null };
    expect(describeApproval({ ...base, summary: "Delete x from your Enzo folder" })).toBe(
      "Delete x from your Enzo folder",
    );
    expect(describeApproval({ ...base, summary: null })).toBe('Run "delete file"');
  });
});
