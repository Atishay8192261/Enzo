import {
  type AccountsClient,
  type ChatClient,
  type ChatsClient,
  type ConversationMemory,
  type Start,
  signInWithChatGPT,
  useChat,
} from "@enzo/chat";
import type { ChatSummary, ProviderConnection } from "@enzo/sdk";
import type { ScrollBoxRenderable } from "@opentui/core";
import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApprovalCard } from "./Approval.tsx";
import { openInBrowser } from "./browser.ts";
import {
  accountWords,
  ago,
  chatTitle,
  Header,
  Home,
  modelWords,
  SIDEBAR_AUTO_WIDTH,
  Sidebar,
  StatusBar,
} from "./Chrome.tsx";
import { type Copy, lastCodeBlock } from "./clipboard.ts";
import {
  type CommandContext,
  echoOf,
  type PickerKind,
  type Reply,
  runCommand,
} from "./commands.ts";
import { suggest } from "./complete.ts";
import { KeyPrompt } from "./KeyPrompt.tsx";
import { type Choice, Picker } from "./Picker.tsx";
import { Prompt } from "./Prompt.tsx";
import { type Card, CommandCard, EnzoHeader, Item, ReplyBlock, Thinking } from "./Transcript.tsx";
import { COLOR, markdownStyle } from "./theme.ts";

/** Everything the command palette offers (Ctrl+P), each as the command it runs (#35). */
export const PALETTE: Choice[] = [
  { label: "New chat", description: "this one stays saved", value: "/new" },
  { label: "Choose a model", description: "or a tier, or smart routing", value: "/model" },
  { label: "Connect an account", description: "ChatGPT plan or an API key", value: "/login" },
  { label: "Disconnect an account", value: "/logout" },
  { label: "Accounts", description: "what is connected", value: "/connections" },
  { label: "Models by tier", description: "fast, balanced, powerful", value: "/models" },
  { label: "Copy the last reply", value: "/copy" },
  { label: "Copy the last code block", value: "/copy code" },
  { label: "Send my last message again", description: "if it did not go through", value: "/retry" },
  { label: "Show or hide the side panel", value: "/sidebar" },
  { label: "Message details", description: "show or hide times, tokens, cost", value: "/details" },
  { label: "Help", description: "commands and keys", value: "/help" },
  { label: "Quit", description: "or type exit, or ctrl+d", value: "/quit" },
];

const PICKER_TITLE: Record<PickerKind | "palette", string> = {
  palette: "Commands",
  model: "Choose a model",
  login: "Connect an account",
  logout: "Disconnect an account",
  chats: "Your chats",
  delete: "Move this chat to the trash?",
};

/** Typed alone and sent with Enter while an approval card is open: the answer to the card. */
export const APPROVAL_WORDS: Record<string, boolean> = { y: true, yes: true, n: false, no: false };

/** Typing one of these words alone quits, like a shell (no slash needed). */
const QUIT_WORDS = new Set(["exit", "quit"]);

/** A card that a running command can show and then update (sign-in progress, key checks). */
interface CardHandle {
  show(reply: Exclude<Reply, null>): void;
}

export function App({
  client,
  memory,
  onQuit,
  openUrl = openInBrowser,
  copy = async () => false,
  version = "dev",
  gatewayUrl = "http://127.0.0.1:8787",
  healthEveryMs,
  start = "fresh",
}: {
  client: ChatClient & AccountsClient & ChatsClient;
  memory: ConversationMemory;
  onQuit: () => void;
  /** Opens sign-in pages; tests pass a fake. */
  openUrl?: (url: string) => Promise<void>;
  /** Puts text on the clipboard; tests pass a fake. */
  copy?: Copy;
  version?: string;
  /** Where the gateway is, for the offline message. */
  gatewayUrl?: string;
  /** How often to check the gateway; tests make it short. */
  healthEveryMs?: number;
  /** A fresh chat (`enzo`), or the last one (`enzo --continue`) (#57). */
  start?: Start;
}) {
  const chat = useChat(client, memory, "cli", {
    start,
    ...(healthEveryMs ? { healthEveryMs } : {}),
  });
  const { state, send, resend, decide, chooseBrain, refreshBrains, newChat, openChat, stop } = chat;
  const renderer = useRenderer();
  const { width, height } = useTerminalDimensions();
  const markdown = useMemo(() => markdownStyle(renderer.nativeScene), [renderer]);
  const scroll = useRef<ScrollBoxRenderable | null>(null);

  const [cards, setCards] = useState<Card[]>([]);
  const nextCard = useRef(0);
  const [login, setLogin] = useState<{ provider: string; card: CardHandle } | null>(null);
  const [picker, setPicker] = useState<PickerKind | "palette" | null>(null);
  const [sidebar, setSidebar] = useState<boolean | null>(null); // null: by terminal width
  const [details, setDetails] = useState(true);
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  // Past chats: the newest one for the opening screen's "continue" line, all of them for /chats.
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [flash, setFlash] = useState<string | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const approval = state.approvals[0];
  const showSidebar = sidebar ?? width >= SIDEBAR_AUTO_WIDTH;

  const say = useCallback((text: string) => {
    setFlash(text);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlash(null), 2500);
  }, []);
  useEffect(() => () => void (flashTimer.current && clearTimeout(flashTimer.current)), []);

  // Accounts drive the status bar, the opening screen and completion: load them when the chat
  // opens, when the gateway is back, and whenever a command connects or disconnects one.
  const loadAccounts = useCallback(() => {
    client.connections().then(setConnections, () => {});
  }, [client]);
  const opened = state.status !== "connecting" && state.status !== "offline";
  useEffect(() => {
    if (opened && state.online) loadAccounts();
  }, [opened, state.online, loadAccounts]);
  const loadChats = useCallback(() => {
    client.conversations(30).then(setChats, () => {});
  }, [client]);
  useEffect(() => {
    if (opened && state.online) loadChats();
  }, [opened, state.online, loadChats]);
  const lastChat = chats.find((c) => c.id !== state.conversationId);
  const accountsChanged = useCallback(() => {
    loadAccounts();
    refreshBrains().catch(() => {});
  }, [loadAccounts, refreshBrains]);

  const cardFor = (command: string): CardHandle => {
    const id = nextCard.current++;
    const at = state.items.length;
    return {
      show(reply) {
        setCards((all) => {
          const card: Card = { id, at, command, reply };
          return all.some((c) => c.id === id)
            ? all.map((c) => (c.id === id ? card : c))
            : [...all, card];
        });
        scroll.current?.scrollTo(Number.POSITIVE_INFINITY);
      },
    };
  };

  const copyLast = async (what: "reply" | "code"): Promise<Reply> => {
    const reply = state.items.findLast((i) => i.role === "assistant")?.text;
    const text = reply && what === "code" ? lastCodeBlock(reply) : reply;
    if (!text) {
      return {
        text: what === "code" ? "No code block in Enzo's last reply." : "No reply to copy yet.",
        error: true,
      };
    }
    if (!(await copy(text))) return { text: "Could not reach the clipboard.", error: true };
    const lines = text.split("\n").length;
    return `Copied ${what === "code" ? "the last code block" : "the last reply"} (${lines} line${lines === 1 ? "" : "s"}).`;
  };

  const contextFor = (card: CardHandle): CommandContext => ({
    client,
    current: state.brain,
    chatId: state.conversationId,
    chooseBrain,
    newChat: async () => {
      await newChat();
      setCards([]);
    },
    startLogin: (provider) => setLogin({ provider, card }),
    signInWithChatGPT: async (newAccount) => {
      card.show("Opening your browser to sign in with ChatGPT…");
      const result = await signInWithChatGPT(client, {
        newAccount,
        open: openUrl,
        // The link gets its own line: a terminal must never wrap it into other words.
        waiting: (url) =>
          card.show(
            `Finish signing in with ChatGPT in your browser. If no page opened, go to:\n${url}`,
          ),
      });
      if (!result.ok) return { text: result.text, error: true };
      accountsChanged();
      return result.usageUrl ? `${result.text}\nManage usage:\n${result.usageUrl}` : result.text;
    },
    pick: (kind) => {
      if (kind === "chats") loadChats();
      setPicker(kind);
    },
    openChat: async (id) => {
      await openChat(id);
      setCards([]);
    },
    renameChat: async (title) => {
      const id = state.conversationId;
      if (!id) return "Send a message first: a new chat is named after its first message.";
      const name = title.split(/\s+/).join(" ");
      await client.renameConversation(id, name);
      chat.renamed(name);
      loadChats();
      return `Renamed this chat “${name}”.`;
    },
    deleteChat: async () => {
      const id = state.conversationId;
      if (!id) return "Nothing to delete: this chat has no messages yet.";
      const name = chatTitle(state, 40);
      await client.deleteConversation(id);
      await newChat();
      setCards([]);
      loadChats();
      // The fresh chat's opening screen shows; a card would hide it.
      say(`Moved “${name}” to the trash`);
      return null;
    },
    copy: copyLast,
    toggleSidebar: () => setSidebar(!showSidebar),
    setDetails: (on) => {
      const next = on ?? !details;
      setDetails(next);
      return next;
    },
    resend,
    accountsChanged,
    quit: onQuit,
  });

  const runLine = async (line: string) => {
    const card = cardFor(echoOf(line));
    const reply = await runCommand(line, contextFor(card));
    if (reply !== null) card.show(reply);
  };

  const saveKey = (provider: string, key: string, card: CardHandle) => {
    setLogin(null);
    card.show(`Checking your key with ${provider}…`);
    void (async () => {
      try {
        await client.connect(provider, key);
        card.show(`Connected ${provider}. The key is stored in the vault, never in chats or logs.`);
        accountsChanged();
      } catch (e: unknown) {
        card.show({ text: e instanceof Error ? e.message : String(e), error: true });
      }
    })();
  };

  const submit = (raw: string): boolean => {
    const line = raw.trim();
    if (!line) return true;
    if (QUIT_WORDS.has(line.toLowerCase())) {
      onQuit();
      return true;
    }
    if (line.startsWith("/")) {
      void runLine(line);
      return true;
    }
    if (approval && state.status === "ready") {
      // The card is answered in words, and only with Enter: a message that starts with "y" can
      // never approve by accident. Anything else is a new message, and the card is closed as not
      // approved (#55).
      const answer = APPROVAL_WORDS[line.toLowerCase()];
      if (answer !== undefined) {
        void decide(approval.id, answer);
        return true;
      }
    }
    if (state.status === "streaming") {
      // Kept in the input, never lost: it can go as soon as the reply is done.
      say("Enzo is still replying: esc stops it, or send when it's done.");
      return false;
    }
    scroll.current?.scrollTo(Number.POSITIVE_INFINITY);
    void send(line);
    return true;
  };

  // Keys for the whole app. The input, the slash popup and dialogs see each key first and
  // mark the ones they used, so a key does one thing only.
  const latest = useRef({ approval, state, stop, decide, login, picker });
  latest.current = { approval, state, stop, decide, login, picker };
  useKeyboard((key) => {
    const now = latest.current;
    if (key.ctrl && key.name === "d") {
      key.preventDefault();
      return onQuit();
    }
    if (key.defaultPrevented || now.login || now.picker) return;
    if (key.ctrl && key.name === "p") {
      key.preventDefault();
      return setPicker("palette");
    }
    if (key.ctrl && key.name === "r") {
      // Past chats, like Claude Code's /resume (#38).
      key.preventDefault();
      loadChats();
      return setPicker("chats");
    }
    if (key.ctrl && key.name === "c") {
      key.preventDefault();
      const selected = renderer.getSelection()?.getSelectedText();
      if (selected) return void copySelection(selected);
      return onQuit(); // the input was empty (it clears itself otherwise)
    }
    if (key.name === "escape") {
      void now.stop().then((stopping) => {
        if (stopping) say("Stopping the reply…");
      });
      return;
    }
    if (key.name === "pageup" || key.name === "pagedown") {
      const page = Math.max(1, Math.floor((scroll.current?.height ?? 10) / 2));
      scroll.current?.scrollBy(key.name === "pageup" ? -page : page);
      return;
    }
  });

  const copySelection = async (text: string) => {
    say((await copy(text)) ? "Copied to clipboard" : "Could not reach the clipboard");
  };

  const choices = (kind: PickerKind | "palette"): Choice[] => {
    if (kind === "palette") return PALETTE;
    if (kind === "chats") {
      return chats.map((c) => ({
        label: c.title ?? "Untitled chat",
        description: `${ago(c.last_active)} · ${c.messages} message${c.messages === 1 ? "" : "s"}${c.id === state.conversationId ? " · open now" : ""}`,
        value: `/open ${c.id}`,
      }));
    }
    if (kind === "delete") {
      // Keeping it is first: a stray Enter never deletes anything.
      return [
        { label: "No, keep it", value: "/delete keep" },
        { label: `Yes, move “${chatTitle(state, 30)}” to the trash`, value: "/delete confirm" },
      ];
    }
    const sources = { connections, brains: state.brains };
    return suggest(`/${kind} `, sources).map((s) => {
      const brain = state.brains.find((b) => b.name === s.label);
      return {
        label: s.label,
        description: s.description,
        value: s.value,
        ...(kind === "model" ? { group: brain ? `${brain.tier} models` : "Routing" } : {}),
        ...(brain && !brain.available ? { muted: true } : {}),
      };
    });
  };

  const offline = !state.online || state.status === "offline";
  const host = gatewayUrl.replace(/^https?:\/\//, "").replace(/\/+$/, "");
  const banner = offline ? (
    <box flexDirection="column" paddingLeft={3} paddingTop={1} flexShrink={0}>
      <text fg={COLOR.error}>Can't reach Enzo at {host}. Retrying every few seconds…</text>
      <text fg={COLOR.dim}>If it stays down, quit (ctrl+d) and run enzo again.</text>
    </box>
  ) : null;

  const history = state.items.filter((i) => i.role === "user").map((i) => i.text);
  const busy = state.status === "streaming";
  const prompt = login ? (
    <KeyPrompt
      provider={login.provider}
      onSubmit={(key) => saveKey(login.provider, key, login.card)}
      onCancel={() => {
        login.card.show("Login cancelled. Nothing was saved.");
        setLogin(null);
      }}
    />
  ) : (
    <Prompt
      // Typing always works, also while a card waits for an answer (#55).
      focused={!picker}
      placeholder={
        approval
          ? "y approves · n declines · or type a new message"
          : busy
            ? "Enzo is working… (esc stops it)"
            : "Message Enzo, or / for commands"
      }
      history={history}
      sources={{ connections, brains: state.brains }}
      meta={`${modelWords(state)} · ${accountWords(connections)}`}
      onSubmit={submit}
    />
  );

  // `enzo --continue` waits for its chat instead of drawing the opening screen first (#57).
  const empty =
    !(start === "last" && state.status === "connecting") &&
    state.items.length === 0 &&
    !state.draft &&
    cards.length === 0 &&
    !approval &&
    state.status !== "streaming";
  const running = state.items.some((i) => i.status === "running");

  const transcript: ReactNode[] = [];
  const cardsAt = (i: number) =>
    cards.filter((c) => (i < 0 ? c.at >= state.items.length : c.at === i));
  state.items.forEach((item, i) => {
    for (const card of cardsAt(i))
      transcript.push(<CommandCard key={`card-${card.id}`} card={card} />);
    // Enzo's side of each turn starts with its name; steps and words hang under it.
    const previous = state.items[i - 1];
    // A message that never went out gets no reply header: nothing answered it.
    if (
      item.role !== "user" &&
      (previous === undefined || (previous.role === "user" && !previous.unsent))
    ) {
      // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
      transcript.push(<EnzoHeader key={`lead-${i}`} />);
    }
    // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
    transcript.push(<Item key={`item-${i}`} item={item} markdown={markdown} details={details} />);
  });
  for (const card of cardsAt(-1))
    transcript.push(<CommandCard key={`card-${card.id}`} card={card} />);
  const waiting = busy && state.items.at(-1)?.role === "user";

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: a terminal box, not a DOM element
    <box
      flexDirection="column"
      width="100%"
      height="100%"
      onMouseUp={() => {
        // Selecting text with the mouse copies it (#35).
        const selected = renderer.getSelection()?.getSelectedText();
        if (selected) void copySelection(selected);
      }}
    >
      {empty ? null : <Header state={state} width={width} />}
      <box flexDirection="row" flexGrow={1} minHeight={0}>
        <box
          flexDirection="column"
          flexGrow={1}
          minWidth={0}
          paddingLeft={2}
          paddingRight={2}
          paddingBottom={1}
        >
          {empty ? (
            <Home
              state={state}
              connections={connections}
              lastChat={lastChat ?? null}
              version={version}
              width={width}
              height={height}
              prompt={
                <>
                  {banner}
                  {prompt}
                </>
              }
            />
          ) : (
            <>
              <scrollbox ref={scroll} flexGrow={1} stickyScroll stickyStart="bottom">
                {transcript}
                {waiting ? <EnzoHeader /> : null}
                {state.draft ? (
                  <ReplyBlock
                    item={{ role: "assistant", text: state.draft }}
                    markdown={markdown}
                    details={false}
                    streaming
                  />
                ) : null}
                {busy && !state.draft && !running ? <Thinking /> : null}
                {state.error ? (
                  <box paddingLeft={3} marginTop={1}>
                    <text fg={COLOR.error}>{state.error}</text>
                  </box>
                ) : null}
                <box height={1} />
              </scrollbox>
              {approval ? <ApprovalCard approval={approval} /> : null}
              {banner}
              <box height={1} />
              {prompt}
            </>
          )}
        </box>
        {showSidebar && !empty ? <Sidebar state={state} connections={connections} /> : null}
      </box>
      <StatusBar state={state} connections={connections} width={width} flash={flash} />
      {picker ? (
        <Picker
          // A new list starts fresh: nothing typed into the last one carries over.
          key={picker}
          title={PICKER_TITLE[picker]}
          choices={choices(picker)}
          onClose={() => setPicker(null)}
          onPick={(choice) => {
            setPicker(null);
            void runLine(choice.value);
          }}
        />
      ) : null}
    </box>
  );
}
