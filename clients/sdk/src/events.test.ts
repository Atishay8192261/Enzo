import { describe, expect, it } from "vitest";
import { MalformedEventError, toAgentEvent } from "./events.ts";

describe("toAgentEvent", () => {
  it("accepts each documented event", () => {
    expect(toAgentEvent("text", '{"text":"hi"}')).toEqual({ type: "text", text: "hi" });
    expect(
      toAgentEvent(
        "approval_needed",
        '{"id":"appr_1","tool":"send_email","arguments":{"to":"bob"},"summary":"Email bob","detail":null}',
      ),
    ).toEqual({
      type: "approval_needed",
      id: "appr_1",
      tool: "send_email",
      arguments: { to: "bob" },
      summary: "Email bob",
      detail: null,
    });
    expect(
      toAgentEvent(
        "done",
        '{"stop":"final_answer","text":"ok","steps":1,"cost_usd":null,"brain":"gpt-sol","model":"gpt-6.2-sol","failure":null,"plan":false,"usage":{}}',
      ),
    ).toMatchObject({ type: "done", cost_usd: null, brain: "gpt-sol", model: "gpt-6.2-sol" });
    expect(
      toAgentEvent(
        "done",
        '{"stop":"final_answer","text":"ok","steps":1,"cost_usd":0,"brain":"gpt-sol","model":"m","failure":null,"plan":true,"usage":{}}',
      ),
    ).toMatchObject({ type: "done", plan: true });
    expect(() =>
      toAgentEvent(
        "done",
        '{"stop":"final_answer","text":"ok","steps":1,"cost_usd":0,"brain":"b","model":"m","failure":null,"plan":"yes"}',
      ),
    ).toThrow("malformed done event");
    // a done event from an older gateway (no brain/model) is malformed, not silently partial
    expect(() =>
      toAgentEvent("done", '{"stop":"final_answer","text":"ok","steps":1,"cost_usd":null}'),
    ).toThrow("malformed done event");
  });

  it("carries each step in plain words (#40)", () => {
    expect(
      toAgentEvent("tool_started", '{"id":"c1","tool":"read_file","label":"Reading plan.md"}'),
    ).toEqual({ type: "tool_started", id: "c1", tool: "read_file", label: "Reading plan.md" });
    expect(
      toAgentEvent(
        "tool_finished",
        '{"id":"c1","tool":"read_file","label":"Reading plan.md","status":"ok","latency_ms":3}',
      ),
    ).toMatchObject({ type: "tool_finished", label: "Reading plan.md", status: "ok" });
    // without its words the chat could only show the tool's name, which people never see
    expect(() => toAgentEvent("tool_started", '{"id":"c1","tool":"read_file"}')).toThrow(
      "malformed tool_started event",
    );
  });

  it("says where Enzo is using the browser, and when it closed (ADR 0027)", () => {
    expect(toAgentEvent("browser", '{"site":"shop.example"}')).toEqual({
      type: "browser",
      site: "shop.example",
    });
    expect(toAgentEvent("browser", '{"site":null}')).toEqual({ type: "browser", site: null });
    expect(() => toAgentEvent("browser", '{"site":3}')).toThrow("malformed browser event");
  });

  it("skips events this client does not know (forward compatible)", () => {
    expect(toAgentEvent("future_event", "{}")).toBeUndefined();
  });

  it("fails loudly on malformed data instead of rendering garbage", () => {
    expect(() => toAgentEvent("text", "not json")).toThrow(MalformedEventError);
    expect(() => toAgentEvent("text", '{"text":5}')).toThrow(MalformedEventError);
    expect(() => toAgentEvent("approval_needed", '{"id":"a","tool":"t","arguments":[]}')).toThrow(
      MalformedEventError,
    );
  });
});
