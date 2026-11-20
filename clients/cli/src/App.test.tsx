import { describe, expect, it, mock } from "bun:test";
import { mkdtempSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GatewayError } from "@enzo/sdk";
import { TextAttributes } from "@opentui/core";
import { act } from "react";
import { fileMemory } from "./memory.ts";
import {
  arrow,
  done,
  enter,
  events,
  fakeClient,
  lines,
  memory,
  paste,
  press,
  type Screen,
  screen,
  sender,
  settle,
  slowly,
  type,
  USED,
  until,
} from "./testing.tsx";

/** Whether some run of `text` on screen is drawn bold. */
function bold(s: Screen, text: string): boolean {
  return s
    .captureSpans()
    .lines.some((line) =>
      line.spans.some(
        (span) => span.text.includes(text) && (span.attributes & TextAttributes.BOLD) !== 0,
      ),
    );
}

describe("a chat in the terminal", () => {
  it("sends a message and streams the reply as formatted Markdown, not raw (#36)", async () => {
    const reply = "A **mutex** lets *one* thread in.\n\n- call `lock()`\n- then `unlock()`";
    const send = mock(() =>
      slowly(
        20,
        { type: "text", text: reply.slice(0, 16) },
        { type: "text", text: reply.slice(16) },
        done({ text: reply, usage: USED }),
      ),
    );
    await using s = await screen(fakeClient({ send }));
    await enter(s, "explain a mutex");
    await until(s, "└");
    const frame = await until(s, "then unlock()");
    expect(send).toHaveBeenCalledWith(
      "c1",
      "explain a mutex",
      expect.objectContaining({ device: "cli" }),
    );
    expect(frame).toContain("A mutex lets one thread in.");
    expect(frame).not.toContain("**"); // no raw Markdown markers
    expect(frame).not.toContain("`lock()`");
    expect(frame).toContain("- call lock()");
    expect(bold(s, "mutex")).toBe(true);
    expect(frame).toContain("◆ enzo"); // Enzo's side of the turn is named
    expect(frame).toContain("╭─ you"); // the user's message is its own card
  });

  it("says who answered on its own line, never glued to the reply (#36)", async () => {
    const send = () => events(done({ text: "Paris", brain: "claude-sonnet", usage: USED }));
    await using s = await screen(fakeClient({ send }));
    await enter(s, "Capital of France?");
    const frame = await until(s, "└ claude-sonnet-5-5");
    const reply = lines(frame).find((l) => l.includes("Paris")) ?? "";
    expect(reply.trim()).toBe("Paris");
  });

  it("shows when a message was sent and what each reply used (#49)", async () => {
    const send = () =>
      slowly(30, { type: "text", text: "Paris" }, done({ text: "Paris", usage: USED }));
    await using s = await screen(fakeClient({ send }), { width: 130 });
    await enter(s, "Capital of France?");
    const frame = await until(s, "1,388 in · 81 out");
    expect(frame).toMatch(/you · \d{1,2}[:.]\d{2}/); // the time it was sent
    expect(frame).toMatch(/first word \d+ms/);
    expect(frame).toContain("$0.0021");
    expect(frame).toContain("this chat: 1,469 tokens · $0.0021"); // the status bar adds it up
    expect(frame).toContain("tokens in"); // and the side panel has the breakdown
    await enter(s, "/details off");
    const quiet = await until(s, "Details off.");
    expect(quiet).not.toContain("1,388 in · 81 out");
    expect(quiet).toContain("└ claude-sonnet-5-5"); // who answered still shows
    expect(quiet).toContain("this chat: 1,469 tokens");
  });

  it("a reopened chat shows each step and what each reply used (#40, #49)", async () => {
    const client = fakeClient({
      messages: async () => [
        { role: "user", parts: [{ type: "text", text: "tidy notes.md" }] },
        {
          role: "assistant",
          parts: [
            {
              type: "tool_call",
              id: "a",
              name: "read_file",
              label: "Reading notes.md",
              latency_ms: 40,
            },
          ],
        },
        { role: "tool", parts: [{ type: "tool_result", call_id: "a", content: "{}" }] },
        { role: "assistant", parts: [{ type: "text", text: "Tidied." }] },
      ],
      usage: async () => ({
        total: USED,
        turns: [{ ...USED, started_at: "2026-11-20T09:05:00Z" }],
      }),
    });
    await using s = await screen(client, { ready: "Tidied.", start: "last" });
    const frame = await until(s, "model time");
    expect(frame).toContain("✓ Reading notes.md  40ms");
    expect(frame).toContain("3.1s model time");
    expect(frame).toContain("1,388 in · 81 out");
    expect(frame).not.toContain("read_file");
  });

  it("shows each step as Enzo takes it, in plain words, and how it went (#40)", async () => {
    let finish = () => {};
    const send = () =>
      (async function* () {
        yield {
          type: "tool_started" as const,
          id: "t1",
          tool: "web_search",
          label: "Searching the web for “mutex”",
        };
        await new Promise<void>((r) => {
          finish = r;
        });
        yield {
          type: "tool_finished" as const,
          id: "t1",
          tool: "web_search",
          label: "Searching the web for “mutex”",
          status: "ok",
          latency_ms: 420,
        };
        yield {
          type: "tool_started" as const,
          id: "t2",
          tool: "read_page",
          label: "Reading bbc.com/news",
        };
        yield {
          type: "tool_finished" as const,
          id: "t2",
          tool: "read_page",
          label: "Reading bbc.com/news",
          status: "failed",
          latency_ms: 30,
        };
        yield done({ text: "Here it is." });
      })();
    await using s = await screen(fakeClient({ send }));
    await enter(s, "news about mutexes");
    const running = await until(s, "Searching the web for “mutex”…");
    expect(running).toMatch(/[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] Searching the web/); // a spinner while it runs
    expect(running).not.toContain("Thinking…");
    finish();
    const after = await until(s, "Here it is.");
    expect(after).toContain("✓ Searching the web for “mutex”  420ms");
    expect(after).toContain("✗ Reading bbc.com/news · failed");
    expect(after).not.toMatch(/web_search|read_page/); // never a tool's name (§10.3)
  });

  it("asks for approval of the exact action and resumes on y", async () => {
    const decide = mock(async () => {});
    const client = fakeClient({
      decide,
      send: () =>
        events(
          {
            type: "approval_needed",
            id: "appr_1",
            tool: "send_email",
            summary: "Email bob@example.com",
            detail: Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join("\n"),
            arguments: { to: "bob@example.com" },
          },
          done({ stop: "approval_pending", text: "Waiting" }),
        ),
      resume: () => events(done({ text: "Sent." })),
    });
    await using s = await screen(client, { height: 50 });
    await enter(s, "email bob");
    const card = await until(s, "Approve this action?");
    expect(card).toContain("Email bob@example.com"); // plain words, not the tool name (§10.3)
    expect(card).not.toContain("send_email");
    expect(card).toContain("line 12"); // the detail, clipped so the card stays readable
    expect(card).not.toContain("line 13");
    expect(card).toContain("28 more lines");
    expect(card).toContain('"to": "bob@example.com"'); // and the exact action (§12.3)
    expect(card).toContain("y + enter approves");
    await enter(s, "y");
    await until(s, "Sent.");
    expect(decide).toHaveBeenCalledWith("c1", "appr_1", true);
  });

  it("typing while a card is open goes into the input; sending it means not this (#55)", async () => {
    const decide = mock(async () => {});
    const send = sender(() =>
      events(
        {
          type: "approval_needed",
          id: "a1",
          tool: "delete_file",
          arguments: { path: "t39.txt" },
          summary: "Delete t39.txt",
          detail: null,
        },
        done({ stop: "approval_pending", text: "Waiting" }),
      ),
    );
    await using s = await screen(fakeClient({ send, decide }), { height: 44 });
    await enter(s, "delete t39.txt");
    await until(s, "Approve this action?");
    await type(s, "what is 2+2");
    expect(await until(s, "❯ what is 2+2")).toContain("Delete t39.txt"); // seen, not dropped
    send.mockImplementation(() => events(done({ text: "4" })));
    await act(async () => {
      s.mockInput.pressEnter();
    });
    const after = await until(s, "Not approved: Delete t39.txt (you sent a new message)");
    expect(send.mock.calls.at(-1)?.[1]).toBe("what is 2+2");
    expect(decide).not.toHaveBeenCalled();
    expect(after).not.toContain("Approve this action?");
  });

  it("a message that only starts with y never approves; n + Enter declines (#55)", async () => {
    const decide = mock(async () => {});
    const card = () =>
      events(
        {
          type: "approval_needed",
          id: "a1",
          tool: "delete_file",
          arguments: { path: "a.md" },
          summary: "Delete a.md",
          detail: null,
        },
        done({ stop: "approval_pending", text: "Waiting" }),
      );
    const send = sender(card);
    await using s = await screen(fakeClient({ send, decide }), { height: 44 });
    await enter(s, "delete a.md");
    await until(s, "Approve this action?");
    await type(s, "y");
    expect(decide).not.toHaveBeenCalled(); // one letter alone does nothing yet
    await type(s, "esterday I wrote it");
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, "Not approved: Delete a.md");
    expect(decide).not.toHaveBeenCalled();
    expect(send.mock.calls.at(-1)?.[1]).toBe("yesterday I wrote it");
    await until(s, "Approve this action?"); // the fake asks again
    await enter(s, "no");
    await until(s, "Declined: Delete a.md");
    expect(decide).toHaveBeenCalledWith("c1", "a1", false);
  });

  it("a reopened chat shows the approval left open, and y answers it (#39)", async () => {
    const decide = mock(async () => {});
    const conversation = async () => ({
      id: "c1",
      kind: "main" as const,
      title: null,
      brain: null,
      pending_approvals: [
        {
          id: "a1",
          tool: "delete_file",
          arguments: { path: "plan.md" },
          summary: "Delete plan.md",
          detail: null,
        },
      ],
    });
    await using s = await screen(fakeClient({ conversation, decide }), {
      memory: memory("c1"),
      start: "last",
      ready: "Approve this action?",
    });
    expect(s.captureCharFrame()).toContain("Delete plan.md");
    await enter(s, "y");
    await until(s, "Approved: Delete plan.md");
    expect(decide).toHaveBeenCalledWith("c1", "a1", true);
  });

  it("marks a refused message not sent and stops the running reply with Esc (#46)", async () => {
    const stop = mock(async () => true);
    const send = () =>
      (async function* () {
        yield* [];
        throw new GatewayError("/turns failed: HTTP 409: busy", 409);
      })();
    await using s = await screen(fakeClient({ send, stop }));
    await enter(s, "are you back?");
    const frame = await until(s, "you · not sent");
    expect(frame).toContain("Not sent: Enzo is still finishing the last reply.");
    expect(frame).toContain("Esc stops it.");
    expect(frame).not.toContain("HTTP 409"); // never a raw status or URL
    expect(frame).not.toContain("◆ enzo"); // nothing answered it, so no reply header
    await press(s, "ESCAPE");
    await until(s, "Stopping the reply…");
    expect(stop).toHaveBeenCalledWith("c1");
  });

  it("when the stream ends early, the chat stops waiting and keeps what arrived (#46)", async () => {
    const send = () => events({ type: "text", text: "Once upon a time" });
    await using s = await screen(fakeClient({ send }));
    await enter(s, "a story");
    const frame = await until(s, "Lost the connection to Enzo mid-reply.");
    expect(frame).toContain("Once upon a time");
    expect(frame).toContain("Message Enzo"); // ready to type again
  });

  it("keeps what is typed while a reply streams, and never loses it (#35)", async () => {
    let finish = () => {};
    const send = sender(() =>
      (async function* () {
        yield { type: "text" as const, text: "Working on it" };
        await new Promise<void>((r) => {
          finish = r;
        });
        yield done({ text: "Working on it", usage: USED });
      })(),
    );
    await using s = await screen(fakeClient({ send }));
    await enter(s, "first");
    await until(s, "Working on it");
    await type(s, "second"); // the input stays focused while Enzo writes
    await act(async () => {
      s.mockInput.pressEnter();
    });
    const frame = await until(s, "Enzo is still replying");
    expect(frame).toContain("❯ second"); // kept, not sent and not thrown away
    expect(send).toHaveBeenCalledTimes(1);
    finish();
    await until(s, "└ claude-sonnet-5-5");
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, /you[\s\S]*you/);
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[1]?.[1]).toBe("second");
  });
});

describe("typing and pasting (#35)", () => {
  it("Shift+Enter, Alt+Enter and Ctrl+J add a line; Enter sends it all", async () => {
    const send = sender();
    // A terminal that tells Shift+Enter apart (kitty protocol: Kitty, Ghostty, WezTerm, iTerm2).
    await using s = await screen(fakeClient({ send }), { kitty: true });
    await type(s, "one");
    await act(async () => {
      s.mockInput.pressEnter({ shift: true });
    });
    await type(s, "two");
    await act(async () => {
      s.mockInput.pressEnter({ meta: true });
    });
    await type(s, "three");
    await press(s, "j", { ctrl: true });
    await type(s, "four");
    expect(send).not.toHaveBeenCalled();
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, "◆ enzo");
    expect(send.mock.calls[0]?.[1]).toBe("one\ntwo\nthree\nfour");
  });

  it("a backslash before Enter makes a new line in any terminal, like Terminal.app (#58)", async () => {
    const send = sender();
    await using s = await screen(fakeClient({ send }));
    await type(s, "line one\\");
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await type(s, "line two");
    expect(send).not.toHaveBeenCalled();
    const frame = await until(s, "line two");
    expect(frame).not.toContain("line one\\"); // the backslash is replaced, not kept
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, "◆ enzo");
    expect(send.mock.calls[0]?.[1]).toBe("line one\nline two");
  });

  it("the hint names a new-line key that works in this terminal (#58)", async () => {
    await using plain = await screen(fakeClient());
    expect(plain.captureCharFrame()).toContain("\\ then enter new line");
    expect(plain.captureCharFrame()).not.toContain("shift+enter new line");
    await using kitty = await screen(fakeClient(), {
      kitty: true,
      terminal: { kitty_keyboard: true },
    });
    await until(kitty, "shift+enter new line");
  });

  it("in a terminal that can't tell Shift+Enter apart, Alt+Enter and Ctrl+J still add a line", async () => {
    const send = sender();
    await using s = await screen(fakeClient({ send }));
    await type(s, "one");
    await act(async () => {
      s.mockInput.pressEnter({ meta: true });
    });
    await type(s, "two");
    await press(s, "j", { ctrl: true });
    await type(s, "three");
    expect(send).not.toHaveBeenCalled();
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, "◆ enzo");
    expect(send.mock.calls[0]?.[1]).toBe("one\ntwo\nthree");
  });

  it("a long paste shows as a short tag and is sent in full", async () => {
    const send = sender();
    await using s = await screen(fakeClient({ send }));
    const pasted = Array.from({ length: 42 }, (_, i) => `row ${i + 1}`).join("\r\n");
    await type(s, "summarize ");
    await paste(s, pasted);
    const frame = await until(s, "[Pasted 42 lines]");
    expect(frame).not.toContain("row 2");
    expect(send).not.toHaveBeenCalled(); // pasted line breaks never send the message
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, "◆ enzo");
    expect(send.mock.calls[0]?.[1]).toBe(`summarize ${pasted.replaceAll("\r\n", "\n")}`);
  });

  it("a short paste goes in as typed", async () => {
    const send = sender();
    await using s = await screen(fakeClient({ send }));
    await paste(s, "just this");
    await until(s, "❯ just this");
    await act(async () => {
      s.mockInput.pressEnter();
    });
    await until(s, "◆ enzo");
    expect(send.mock.calls[0]?.[1]).toBe("just this");
  });

  it("Up and Down bring back earlier messages, and the draft returns", async () => {
    const send = sender();
    await using s = await screen(fakeClient({ send }));
    await enter(s, "first message");
    await until(s, "◆ enzo");
    await enter(s, "second message");
    await until(s, "second message");
    await settle(s, 100);
    await arrow(s, "up");
    await until(s, "❯ second message");
    await arrow(s, "up");
    await until(s, "❯ first message");
    await arrow(s, "down");
    await until(s, "❯ second message");
    await arrow(s, "down");
    await until(s, "❯ Message Enzo"); // back to the empty draft
  });

  it("Ctrl+C clears what was typed; on an empty input it quits", async () => {
    const onQuit = mock(() => {});
    await using s = await screen(fakeClient(), { onQuit });
    await type(s, "oops");
    await until(s, "❯ oops");
    await press(s, "c", { ctrl: true });
    await until(s, "❯ Message Enzo");
    expect(onQuit).not.toHaveBeenCalled();
    await press(s, "c", { ctrl: true });
    expect(onQuit).toHaveBeenCalledTimes(1);
  });
});

describe("quitting", () => {
  for (const word of ["exit", "quit", "EXIT", "/quit", "/exit"]) {
    it(`typing ${word} quits`, async () => {
      const onQuit = mock(() => {});
      const send = sender();
      await using s = await screen(fakeClient({ send }), { onQuit });
      await enter(s, word);
      await settle(s);
      expect(onQuit).toHaveBeenCalledTimes(1);
      expect(send).not.toHaveBeenCalled(); // never sent to Enzo as a message
    });
  }

  it("Ctrl+D quits, even with something typed", async () => {
    const onQuit = mock(() => {});
    await using s = await screen(fakeClient(), { onQuit });
    await type(s, "half a thought");
    await press(s, "d", { ctrl: true });
    expect(onQuit).toHaveBeenCalledTimes(1);
  });

  it("a sentence that only starts with exit is a message", async () => {
    const onQuit = mock(() => {});
    const send = sender();
    await using s = await screen(fakeClient({ send }), { onQuit });
    await enter(s, "exit the loop early?");
    await until(s, "◆ enzo");
    expect(onQuit).not.toHaveBeenCalled();
    expect(send).toHaveBeenCalledTimes(1);
  });
});

describe("copying (#35)", () => {
  const reply = "Use this:\n\n```sh\nls -la\n```\n\nDone.";

  it("/copy copies the last reply, /copy code its last code block", async () => {
    const copied: string[] = [];
    const copy = async (text: string) => {
      copied.push(text);
      return true;
    };
    await using s = await screen(fakeClient({ send: () => events(done({ text: reply })) }), {
      copy,
    });
    await enter(s, "/copy");
    await until(s, "No reply to copy yet.");
    await enter(s, "list files");
    await until(s, "Done.");
    await enter(s, "/copy");
    await until(s, "Copied the last reply (7 lines).");
    await enter(s, "/copy code");
    await until(s, "Copied the last code block (1 line).");
    expect(copied).toEqual([reply, "ls -la"]);
  });

  it("selecting text with the mouse copies it", async () => {
    const copied: string[] = [];
    const copy = async (text: string) => {
      copied.push(text);
      return true;
    };
    const send = () => events(done({ text: "Selectable words" }));
    const onQuit = mock(() => {});
    await using s = await screen(fakeClient({ send }), { copy, onQuit });
    await enter(s, "hi");
    const frame = await until(s, "Selectable words");
    const row = lines(frame).findIndex((l) => l.includes("Selectable words"));
    const col = (lines(frame)[row] ?? "").indexOf("Selectable");
    await act(async () => {
      await s.mockMouse.drag(col, row, col + 9, row);
    });
    await until(s, "Copied to clipboard");
    expect(copied).toEqual(["Selectable"]);
    // Ctrl+C with text selected copies it again, the way terminals do; it never quits then.
    await press(s, "c", { ctrl: true });
    expect(copied).toEqual(["Selectable", "Selectable"]);
    expect(onQuit).not.toHaveBeenCalled();
  });
});

describe("fileMemory", () => {
  it("remembers the conversation in a private file", () => {
    const dir = mkdtempSync(join(tmpdir(), "enzo-"));
    const m = fileMemory({ ENZO_CONFIG_DIR: dir });
    expect(m.get()).toBeNull();
    m.set("c-42");
    expect(fileMemory({ ENZO_CONFIG_DIR: dir }).get()).toBe("c-42");
    expect(statSync(join(dir, "conversation")).mode & 0o777).toBe(0o600);
  });
});
