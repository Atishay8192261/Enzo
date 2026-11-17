import { describe, expect, it } from "vitest";
import { MalformedEventError, toAgentEvent } from "./events.ts";

describe("toAgentEvent", () => {
  it("accepts each documented event", () => {
    expect(toAgentEvent("text", '{"text":"hi"}')).toEqual({ type: "text", text: "hi" });
    expect(
      toAgentEvent(
        "approval_needed",
        '{"id":"appr_1","tool":"send_email","arguments":{"to":"bob"}}',
      ),
    ).toEqual({
      type: "approval_needed",
      id: "appr_1",
      tool: "send_email",
      arguments: { to: "bob" },
    });
    expect(
      toAgentEvent("done", '{"stop":"final_answer","text":"ok","steps":1,"cost_usd":null}'),
    ).toMatchObject({
      type: "done",
      cost_usd: null,
    });
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
