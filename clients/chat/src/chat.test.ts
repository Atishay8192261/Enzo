import { describe, expect, it } from "vitest";
import {
  type ChatState,
  describeApproval,
  formatCost,
  initialState,
  reduce,
  toItems,
} from "./chat.ts";

const ready = reduce(initialState, {
  type: "connected",
  conversationId: "c1",
  brains: [],
  brain: null,
  items: [],
});

describe("chat reducer", () => {
  it("separates text from different steps of one turn", () => {
    let s = reduce(ready, { type: "user_sent", text: "hi" });
    s = reduce(s, { type: "event", event: { type: "text", text: "Let me check." } });
    s = reduce(s, { type: "event", event: { type: "tool_started", id: "c1", tool: "search" } });
    s = reduce(s, { type: "event", event: { type: "text", text: "Found it." } });
    expect(s.draft).toBe("Let me check.\nFound it.");
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
      },
    });
    expect(s.items).toEqual([
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
      },
    });
    expect(s.items).toEqual([
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
    expect(s.items).toEqual([
      { role: "notice", text: "Approved: Delete plan.md" },
      { role: "notice", text: 'Declined: Run "send email"' },
    ]);
    expect(s.approvals).toEqual([]);
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

  it("tracks running tools without naming them in the chat", () => {
    let s = reduce(ready, {
      type: "event",
      event: { type: "tool_started", id: "t1", tool: "search_web" },
    });
    expect(s.working).toBe(1);
    s = reduce(s, {
      type: "event",
      event: { type: "tool_finished", id: "t1", tool: "search_web", status: "ok", latency_ms: 5 },
    });
    expect(s.working).toBe(0);
    expect(JSON.stringify(s.items)).not.toContain("search_web");
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
      },
    });
    expect(s.approvals).toEqual([
      { id: "appr_1", tool: "send_email", arguments: { to: "bob" }, summary: null, detail: null },
    ]);
    expect(s.items.at(-1)).toEqual({ role: "notice", text: "Waiting for your approval." });
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
      },
    });
    expect(s.items).toEqual([{ role: "notice", text: "I could not get an answer: no API key" }]);
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
