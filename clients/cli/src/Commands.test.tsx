import { describe, expect, it, mock } from "bun:test";
import { Picker } from "./Picker.tsx";
import {
  arrow,
  BRAINS,
  CONNECTIONS,
  CONVERSATION,
  enter,
  fakeClient,
  gone,
  lines,
  memory,
  mount,
  paste,
  press,
  screen,
  sender,
  settle,
  type,
  until,
} from "./testing.tsx";

describe("the slash-command list (#34)", () => {
  it("typing / lists every command with what it does, and filters as you type", async () => {
    await using s = await screen(fakeClient());
    await type(s, "/");
    const all = await until(s, "/connections");
    for (const name of ["/login", "/logout", "/models", "/model ", "/new"]) {
      expect(all).toContain(name);
    }
    expect(all).toContain(" 1/16 "); // more below: the list scrolls with the selection
    for (let i = 0; i < 15; i++) await arrow(s, "down");
    expect(await until(s, " 16/16 ")).toContain("/quit");
    expect(all).toContain("↑↓ choose · tab complete · enter run · esc close");
    await type(s, "lo");
    const filtered = await until(s, "/logout [provider]");
    expect(filtered).toContain("/login [provider|chatgpt]");
    expect(filtered).toContain("your ChatGPT plan, or an API key (typed hidden)"); // whole
    const rows = lines(filtered).filter((l) => l.trim().startsWith("│ /"));
    expect(rows.map((l) => l.trim().split(/\s+/)[1])).toEqual(["/login", "/logout"]);
  });

  it("Tab completes, then the arguments are listed: /login offers each account", async () => {
    await using s = await screen(fakeClient());
    await type(s, "/lo");
    await until(s, "/logout");
    await press(s, "TAB");
    const args = await until(s, "your ChatGPT plan pays, no key needed");
    expect(args).toContain("❯ /login ");
    expect(args).toContain("anthropic");
    expect(args).toContain("connected · anthropic · API key");
    expect(args).toContain("xai");
  });

  it("Enter runs the chosen line; Up/Down choose; Esc closes the list", async () => {
    const chooseBrain = mock(async (_id: string, _choice: string | null) => {});
    const createConversation = mock(
      async (_kind?: string, _title?: string, _brain?: string | null) => CONVERSATION,
    );
    const send = sender();
    await using s = await screen(fakeClient({ chooseBrain, createConversation, send }));
    await type(s, "/model ");
    const list = await until(s, "smart routing across your accounts");
    expect(list).toContain("claude-sonnet");
    expect(list).toContain("needs /login xai"); // grok can't answer until xai is connected
    await arrow(s, "down"); // auto → fast
    await press(s, "RETURN");
    await until(s, "No connected account has a fast model yet.");
    await type(s, "/model b");
    await until(s, "any balanced model: everyday default");
    await press(s, "RETURN");
    await until(s, "Model: any balanced model");
    // A fresh chat holds the choice, and is created with it by its first message (#57).
    expect(chooseBrain).not.toHaveBeenCalled();
    await enter(s, "hello");
    await until(s, "◆ enzo");
    expect(createConversation.mock.calls).toEqual([["main", undefined, "balanced"]]);
    await type(s, "/");
    await until(s, "/connections");
    await press(s, "ESCAPE");
    await gone(s, "/connections");
    expect(s.captureCharFrame()).toContain("❯ /"); // what was typed stays
  });

  it("/logout and /check only offer accounts that are connected", async () => {
    await using s = await screen(fakeClient());
    await type(s, "/logout ");
    const frame = await until(s, "anthropic · API key");
    const list = lines(frame).filter((l) => l.includes("│ "));
    expect(list.some((l) => l.includes("xai"))).toBe(false);
  });

  it("a whole command typed out runs as typed: /model alone opens the model picker", async () => {
    const chooseBrain = mock(async (_id: string, _choice: string | null) => {});
    // An open chat (`enzo --continue`): the choice is saved on it right away.
    await using s = await screen(fakeClient({ chooseBrain }), { start: "last" });
    await enter(s, "/model");
    const picker = await until(s, "Choose a model");
    expect(picker).toContain("Routing");
    expect(picker).toContain("balanced models");
    await type(s, "sonnet"); // the picker filters as you type
    await until(s, /claude-sonnet/);
    await press(s, "RETURN");
    await until(s, "Model: claude-sonnet (claude-sonnet-5-5)");
    expect(chooseBrain.mock.calls).toEqual([["c1", "claude-sonnet"]]);
  });
});

describe("the command palette (#35)", () => {
  it("Ctrl+P lists everything Enzo can do; typing filters; Enter runs it", async () => {
    await using s = await screen(fakeClient());
    await press(s, "p", { ctrl: true });
    const palette = await until(s, "Type to filter");
    for (const label of [
      "New chat",
      "Choose a model",
      "Connect an account",
      "Copy the last reply",
    ]) {
      expect(palette).toContain(label);
    }
    await type(s, "connect");
    const filtered = await settle(s);
    expect(filtered).toContain("Connect an account");
    expect(filtered).not.toContain("Copy the last reply");
    await press(s, "RETURN");
    const accounts = await until(s, "your ChatGPT plan pays, no key needed");
    expect(accounts).toContain("Connect an account"); // a picker of accounts
    await press(s, "ESCAPE");
    await gone(s, "your ChatGPT plan pays, no key needed");
  });

  it("a long label is cut at its column, never run into its description", async () => {
    const choices = [{ label: "x".repeat(40), description: "what it does", value: "/x" }];
    await using s = await mount(
      <Picker title="Test" choices={choices} onPick={() => {}} onClose={() => {}} />,
      { width: 80, height: 12 },
      "what it does",
    );
    const row = lines(s.captureCharFrame()).find((l) => l.includes("what it does")) ?? "";
    expect(row).toMatch(/x{20,28} +what it does/);
    expect(row).not.toMatch(/x{29}/);
  });

  it("Esc closes the palette and nothing runs", async () => {
    const onQuit = mock(() => {});
    await using s = await screen(fakeClient(), { onQuit });
    await press(s, "p", { ctrl: true });
    await until(s, "Type to filter");
    await press(s, "ESCAPE");
    await gone(s, "Type to filter");
    await type(s, "still here");
    await until(s, "❯ still here"); // the input has the keys again
    expect(onQuit).not.toHaveBeenCalled();
  });
});

describe("command output (#36)", () => {
  it("each command shows under its own echo, as a card, not as loose text", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/models");
    const catalog = await until(s, "balanced (everyday default):");
    expect(catalog).toContain("╭─ /models");
    expect(catalog).toContain("★ claude-sonnet (claude-sonnet-5-5)");
    expect(catalog).toContain("grok (grok-4.7) · cost unknown · needs /login xai");
  });

  it("/help keeps each wrapped description under its own column", async () => {
    await using s = await screen(fakeClient(), { width: 80, height: 60 });
    await enter(s, "/help");
    const frame = await until(s, "commands and keys");
    const rows = lines(frame);
    const login = rows.findIndex((l) => l.includes("/login [provider|chatgpt]"));
    const first = rows[login] ?? "";
    const next = rows[login + 1] ?? "";
    const column = first.indexOf("your ChatGPT plan");
    expect(column).toBeGreaterThan(0);
    // the wrapped rest starts under the description, never back at the left edge
    expect(next.slice(0, column).replace(/[│\s]/g, "")).toBe("");
    expect(next.slice(column).trim().length).toBeGreaterThan(0);
    expect(frame).toContain("ctrl+d, or type exit");
    expect(frame).toContain("\\ then enter"); // a new line that works in any terminal (#58)
    expect(frame).toContain("y or n, then enter"); // how to answer a card (#55)
  });

  it("unknown commands and wrong arguments say what to type", async () => {
    await using s = await screen(fakeClient());
    await enter(s, "/brains");
    await until(s, "Unknown command: /brains. Type / to see the commands.");
    await enter(s, "/model ghost");
    await until(s, "Unknown model: ghost. See /models.");
    await enter(s, "/model grok");
    await until(s, "grok needs your xai account: /login xai");
  });
});

describe("accounts", () => {
  const KEY = "fake-xai-000000000000";

  it("logs in at a hidden prompt: the key is never drawn", async () => {
    const connect = mock(async () => {});
    await using s = await screen(fakeClient({ connect }));
    await enter(s, "/login xai");
    await until(s, "Paste your xai API key (hidden)");
    let frames = "";
    await type(s, KEY);
    frames += await until(s, `${KEY.length} characters`);
    await press(s, "RETURN");
    frames += await until(s, "Connected xai.");
    expect(connect).toHaveBeenCalledWith("xai", KEY);
    expect(frames).not.toContain(KEY);
    expect(frames).not.toContain("fake-xai");
    await until(s, "Message Enzo"); // back to chat
  });

  it("takes a bracketed paste of a key and trims the newline", async () => {
    const connect = mock(async () => {});
    await using s = await screen(fakeClient({ connect }));
    await enter(s, "/login xai");
    await until(s, "(hidden)");
    await paste(s, `${KEY}\n`);
    const frame = await until(s, `${KEY.length} characters`);
    expect(frame).not.toContain(KEY);
    expect(frame).not.toContain("[Pasted"); // the chat input never saw it
    await press(s, "RETURN");
    await until(s, "Connected xai.");
    expect(connect).toHaveBeenCalledWith("xai", KEY);
  });

  it("Esc or Ctrl+C cancels a login without saving", async () => {
    const connect = mock(async () => {});
    const onQuit = mock(() => {});
    await using s = await screen(fakeClient({ connect }), { onQuit });
    await enter(s, "/login xai");
    await until(s, "(hidden)");
    await type(s, "abc");
    await until(s, "3 characters");
    await press(s, "ESCAPE");
    await until(s, "Login cancelled. Nothing was saved.");
    await enter(s, "/login xai");
    await until(s, "(hidden)");
    await press(s, "c", { ctrl: true });
    await gone(s, "(hidden)");
    expect(connect).not.toHaveBeenCalled();
    expect(onQuit).not.toHaveBeenCalled(); // Ctrl+C at the key prompt cancels, it doesn't quit
  });

  it("shows the provider's refusal and never the key", async () => {
    const connect = mock(() =>
      Promise.reject(new Error("/connections/xai failed: HTTP 422: xai rejected this key")),
    );
    await using s = await screen(fakeClient({ connect }));
    await enter(s, "/login xai");
    await until(s, "(hidden)");
    await type(s, KEY);
    await press(s, "RETURN");
    const frame = await until(s, "xai rejected this key");
    expect(frame).not.toContain(KEY);
  });

  it("refuses keys typed on the command line and unknown providers", async () => {
    const connect = mock(async () => {});
    await using s = await screen(fakeClient({ connect }));
    await enter(s, `/login xai ${KEY}`);
    const frame = await until(s, "Never type a key on the command line");
    expect(frame).not.toContain(KEY);
    await enter(s, "/login ghost");
    await until(s, "Unknown provider: ghost. Providers: anthropic, xai");
    expect(connect).not.toHaveBeenCalled();
  });

  it("signs in with ChatGPT, keeps the link whole, and the opening screen stops asking (#36)", async () => {
    const opened: string[] = [];
    let polls = 0;
    let signedIn = false;
    const startChatGPTSignIn = mock(async (_newAccount: boolean) => ({
      login_id: "l1",
      url: "http://127.0.0.1:8787/auth/chatgpt/l1",
    }));
    const offline = CONNECTIONS.map((c) => ({ ...c, connected: false, method: null }));
    const client = fakeClient({
      startChatGPTSignIn,
      connections: async () =>
        signedIn
          ? [
              ...offline,
              {
                provider: "openai",
                connected: true,
                method: "chatgpt",
                connected_at: "2026-11-20T09:00:00Z",
                methods: ["api_key", "chatgpt"],
                models: ["gpt-6.2-sol"],
                tools: [],
                purpose: null,
                label: "ada@example.com",
              },
            ]
          : offline,
      chatGPTSignIn: async () => {
        polls += 1;
        signedIn = polls >= 2;
        return {
          status: signedIn ? ("connected" as const) : ("pending" as const),
          message: signedIn ? "Signed in with ChatGPT as ada@example.com." : null,
          first_time: signedIn,
          usage_url: "https://chatgpt.com/settings/usage",
        };
      },
    });
    await using s = await screen(client, {
      openUrl: async (url) => {
        opened.push(url);
      },
    });
    await until(s, "No model account is connected yet");
    await enter(s, "/login chatgpt");
    const waiting = await until(s, "Finish signing in with ChatGPT in your browser");
    expect(opened).toEqual(["http://127.0.0.1:8787/auth/chatgpt/l1"]);
    // Whole on one line, so a click or a copy gets all of it.
    expect(lines(waiting).some((l) => l.includes("│ http://127.0.0.1:8787/auth/chatgpt/l1"))).toBe(
      true,
    );
    const frame = await until(s, "You're using your ChatGPT plan", 5000);
    expect(frame).toContain("ada@example.com");
    expect(lines(frame).some((l) => l.includes("│ https://chatgpt.com/settings/usage"))).toBe(true);
    const after = await until(s, "ChatGPT plan (ada@example.com)"); // the status bar knows
    expect(after).not.toContain("No model account is connected yet");
    expect(startChatGPTSignIn).toHaveBeenCalledWith(false);
  });

  it("explains a ChatGPT sign-in that did not finish", async () => {
    const client = fakeClient({
      chatGPTSignIn: async () => ({
        status: "failed" as const,
        message: "The ChatGPT sign-in was cancelled, so nothing was connected.",
        first_time: false,
        usage_url: "https://chatgpt.com/settings/usage",
      }),
    });
    await using s = await screen(client);
    await enter(s, "/login chatgpt");
    await until(s, "sign-in was cancelled", 5000);
  });

  it("signing out of ChatGPT says whether OpenAI confirmed it, with the link whole", async () => {
    const disconnect = mock(async () => ({ remote_sign_out: "not_confirmed" as const }));
    await using s = await screen(fakeClient({ disconnect }));
    await enter(s, "/logout openai");
    const frame = await until(s, "OpenAI did not confirm it");
    expect(lines(frame).some((l) => l.includes("│ https://chatgpt.com/settings/usage"))).toBe(true);
  });

  it("lists, checks and disconnects", async () => {
    const disconnect = mock(async () => ({ remote_sign_out: null }));
    const checkConnection = mock(async () => ({ ok: false, detail: "xai rejected this key" }));
    await using s = await screen(fakeClient({ disconnect, checkConnection }), { height: 50 });
    await enter(s, "/connections");
    await until(s, "anthropic: connected (API key, since 2026-11-17) · claude-opus-5-5");
    await until(s, "xai: not connected: /login xai · grok-4.7");
    await enter(s, "/check xai");
    await until(s, "xai: xai rejected this key");
    await enter(s, "/logout xai");
    await until(s, "Disconnected xai. Its key was deleted.");
    expect(disconnect).toHaveBeenCalledWith("xai");
  });

  it("/new starts a fresh chat on the opening screen and remembers it", async () => {
    let n = 0;
    const createConversation = mock(async () => ({ ...CONVERSATION, id: `c${++n}` }));
    const mem = memory();
    const client = fakeClient({
      createConversation,
      messages: async () => [{ role: "user", parts: [{ type: "text", text: "earlier question" }] }],
    });
    await using s = await screen(client, { memory: mem, ready: "earlier question", start: "last" });
    await enter(s, "/new");
    const frame = await until(s, "your personal agent");
    expect(frame).not.toContain("earlier question");
    expect(mem.get()).toBe(`c${n}`);
  });

  it("a model that needs an account is dimmed in the picker, with how to connect it", async () => {
    await using s = await screen(fakeClient({ brains: async () => BRAINS }));
    await enter(s, "/model");
    const picker = await until(s, "Choose a model");
    expect(picker).toContain("needs /login xai");
  });
});
