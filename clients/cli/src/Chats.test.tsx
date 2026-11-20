import { describe, expect, it, mock } from "bun:test";
import type { ChatSummary, Conversation, StoredMessage } from "@enzo/sdk";
import { act } from "react";
import { ago } from "./Chrome.tsx";
import {
  CONVERSATION,
  done,
  enter,
  events,
  fakeClient,
  gone,
  memory,
  press,
  type Screen,
  screen,
  sender,
  settle,
  type,
  until,
} from "./testing.tsx";

const WORDMARK = "███████╗ ███╗   ██╗";
const HOUR = 3_600_000;

const PAST: ChatSummary[] = [
  {
    id: "c-old",
    title: "Plan my week",
    brain: null,
    created_at: new Date(Date.now() - 30 * HOUR).toISOString(),
    last_active: new Date(Date.now() - 2 * HOUR).toISOString(),
    messages: 3,
  },
  {
    id: "c-older",
    title: "What is a mutex?",
    brain: null,
    created_at: new Date(Date.now() - 60 * HOUR).toISOString(),
    last_active: new Date(Date.now() - 50 * HOUR).toISOString(),
    messages: 1,
  },
];

const OLD_MESSAGES: StoredMessage[] = [
  { role: "user", parts: [{ type: "text", text: "Plan my week" }] },
  { role: "assistant", parts: [{ type: "text", text: "Monday: gym." }] },
];

describe("starting Enzo (#57)", () => {
  it("`enzo` opens a fresh chat and stays there: no jump into an old one", async () => {
    const messages = mock(async (_id: string) => OLD_MESSAGES);
    const createConversation = mock(
      async (_kind?: string, _title?: string, _brain?: string | null) => CONVERSATION,
    );
    const mem = memory("c-old"); // another window used this chat last
    await using s = await screen(fakeClient({ messages, createConversation }), {
      memory: mem,
      ready: WORDMARK,
    });
    await settle(s, 300);
    expect(s.captureCharFrame()).toContain(WORDMARK); // still the opening screen
    expect(messages).not.toHaveBeenCalled(); // the old chat was never loaded
    expect(createConversation).not.toHaveBeenCalled(); // and no empty chat was made
    await enter(s, "hello");
    await until(s, "◆ enzo");
    expect(createConversation).toHaveBeenCalledTimes(1); // made by the first message
    expect(mem.get()).toBe("c1"); // and remembered for `enzo --continue`
  });

  it("`enzo --continue` opens the last chat without drawing the opening screen first", async () => {
    let open = () => {};
    const conversation = mock(
      () =>
        new Promise<Conversation>((resolve) => {
          open = () => resolve({ ...CONVERSATION, id: "c-old", title: "Plan my week" });
        }),
    );
    await using s = await screen(fakeClient({ conversation, messages: async () => OLD_MESSAGES }), {
      memory: memory("c-old"),
      start: "last",
      ready: "",
    });
    await settle(s, 200);
    expect(s.captureCharFrame()).not.toContain(WORDMARK); // no flash of the opening screen
    open();
    const frame = await until(s, "Monday: gym.");
    expect(frame).toContain("◆ ENZO  │  Plan my week");
  });

  it("the opening screen offers to continue the last chat", async () => {
    const client = fakeClient({ conversations: async () => PAST });
    await using s = await screen(client, { ready: "Continue “Plan my week”" });
    expect(s.captureCharFrame()).toContain(
      `(${ago(PAST[0]?.last_active ?? "")}): ctrl+r or /chats`,
    );
  });
});

describe("past chats (#38)", () => {
  async function chats(s: Screen, how: "ctrl+r" | "/chats") {
    if (how === "ctrl+r") await press(s, "r", { ctrl: true });
    else await enter(s, "/chats");
    return until(s, "Your chats");
  }

  for (const how of ["ctrl+r", "/chats"] as const) {
    it(`${how} lists them newest first, and Enter reopens one`, async () => {
      const conversation = mock(async (id: string) => ({
        ...CONVERSATION,
        id,
        title: "Plan my week",
      }));
      const mem = memory();
      await using s = await screen(
        fakeClient({
          conversations: async () => PAST,
          conversation,
          messages: async () => OLD_MESSAGES,
        }),
        { memory: mem },
      );
      const list = await chats(s, how);
      expect(list).toContain("Plan my week");
      expect(list).toContain("2 h ago · 3 messages");
      expect(list).toContain("What is a mutex?");
      expect(list.indexOf("Plan my week")).toBeLessThan(list.indexOf("What is a mutex?"));
      await press(s, "RETURN");
      const frame = await until(s, "Monday: gym.");
      expect(conversation).toHaveBeenCalledWith("c-old");
      expect(frame).toContain("◆ ENZO  │  Plan my week");
      expect(mem.get()).toBe("c-old"); // `enzo --continue` would open it next time
    });
  }

  it("typing filters the list, and Esc closes it", async () => {
    await using s = await screen(fakeClient({ conversations: async () => PAST }));
    await chats(s, "/chats");
    await type(s, "mutex");
    const frame = await settle(s);
    expect(frame).toContain("What is a mutex?");
    expect(frame).not.toContain("Plan my week ");
    await press(s, "ESCAPE");
    await gone(s, "Your chats");
  });

  it("/rename names this chat; a fresh chat says to send a message first", async () => {
    const renameConversation = mock(async (_id: string, _title: string) => {});
    const send = sender(() => events(done({ text: "ok" })));
    await using s = await screen(fakeClient({ renameConversation, send }));
    await enter(s, "/rename Weekly plan");
    await until(s, "Send a message first: a new chat is named after its first message.");
    expect(renameConversation).not.toHaveBeenCalled();
    await enter(s, "plan my week");
    await until(s, "◆ enzo");
    await enter(s, "/rename   Weekly   plan ");
    await until(s, "Renamed this chat “Weekly plan”.");
    expect(renameConversation).toHaveBeenCalledWith("c1", "Weekly plan");
    expect(await until(s, "◆ ENZO  │  Weekly plan")).toContain("╭─ /rename Weekly plan");
    await enter(s, "/rename");
    await until(s, "Usage: /rename <name>");
  });

  it("/delete asks first; yes moves the chat to the trash and starts a fresh one", async () => {
    const deleteConversation = mock(async (_id: string) => {});
    const send = sender(() => events(done({ text: "ok" })));
    await using s = await screen(fakeClient({ deleteConversation, send }));
    await enter(s, "/delete");
    await until(s, "Nothing to delete: this chat has no messages yet.");
    await enter(s, "plan my week");
    await until(s, "◆ enzo");
    await enter(s, "/delete");
    await until(s, "Move this chat to the trash?");
    await press(s, "RETURN"); // the first choice: no, keep it
    await gone(s, "Move this chat to the trash?");
    expect(deleteConversation).not.toHaveBeenCalled();
    await enter(s, "/delete");
    await until(s, "Yes, move “plan my week” to the trash");
    await act(async () => {
      s.mockInput.pressArrow("down");
    });
    await press(s, "RETURN");
    const frame = await until(s, WORDMARK); // a fresh chat, on the opening screen
    expect(deleteConversation).toHaveBeenCalledWith("c1");
    expect(frame).toContain("Moved “plan my week” to the trash");
  });
});
