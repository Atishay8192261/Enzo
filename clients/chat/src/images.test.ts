import type { Brain, StoredMessage } from "@enzo/sdk";
import { describe, expect, it } from "vitest";
import { canSeeImages, initialState, reduce, toItems, userText } from "./chat.ts";

const brain = (name: string, vision: boolean, available = true): Brain => ({
  name,
  provider: name,
  model: `${name}-1`,
  priced: true,
  tier: "balanced",
  recommended: true,
  note: null,
  available,
  vision,
});

describe("images in the chat (ADR 0033)", () => {
  it("shows [image] for each image the words do not already name", () => {
    expect(userText({ text: "what is this?", images: ["img_1"] })).toBe("what is this?\n[image]");
    expect(userText({ text: "compare [Image 1] and [Image 2]", images: ["a", "b"] })).toBe(
      "compare [Image 1] and [Image 2]",
    );
    expect(userText({ text: "", images: ["a", "b"] })).toBe("[image] [image]");
    expect(userText({ text: "hi" })).toBe("hi");
  });

  it("knows before sending whether the chat's model can see", () => {
    const brains = [brain("claude", true), brain("grok", false)];
    expect(canSeeImages({ brains, brain: null })).toBe(true);
    expect(canSeeImages({ brains, brain: "grok" })).toBe(false);
    expect(canSeeImages({ brains, brain: "claude" })).toBe(true);
    expect(canSeeImages({ brains: [brain("claude", true, false)], brain: null })).toBe(false);
    expect(canSeeImages({ brains, brain: "powerful" })).toBe(false); // no powerful one sees
  });

  it("keeps the images a message went with, for showing and for sending again", () => {
    const ready = reduce(initialState, {
      type: "connected",
      conversationId: "c1",
      brains: [],
      brain: null,
      items: [],
    });
    const sent = reduce(ready, { type: "user_sent", text: "look", images: ["img_1"] });
    expect(sent.items.at(-1)).toMatchObject({ role: "user", text: "look", images: ["img_1"] });
  });

  it("a reopened chat shows a message's images, also one sent without words", () => {
    const stored: StoredMessage[] = [
      {
        role: "user",
        parts: [
          { type: "text", text: "" },
          { type: "image", id: "img_1", width: 10, height: 10 },
          { type: "text", text: "[sent 2026-11-27T10:00:00+00:00; timezone: UTC; device: cli]" },
        ],
      },
      { role: "assistant", parts: [{ type: "text", text: "A cat." }] },
    ];
    const [user, reply] = toItems(stored);
    expect(user).toEqual({ role: "user", text: "", images: ["img_1"] });
    expect(userText({ text: user?.text ?? "", images: user?.images ?? [] })).toBe("[image]");
    expect(reply).toMatchObject({ role: "assistant", text: "A cat." });
  });
});
