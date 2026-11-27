import {
  type AccountsClient,
  browserSign,
  type ChatClient,
  type ChatsClient,
  type ConversationMemory,
  type GoalsClient,
  type HelpersClient,
  type MemoryClient,
  type PrivacyClient,
  type ScheduleClient,
  type Start,
  signInWithChatGPT,
  signInWithGoogle,
  useChat,
} from "@enzo/chat";
import type {
  ChatSummary,
  GoogleAccess,
  Helper,
  PrivacyRule,
  ProviderConnection,
  Remembered,
  Scheduled,
  Watch,
  WatchCheck,
} from "@enzo/sdk";
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
  checkReply,
  echoOf,
  helperLine,
  memoryLine,
  type PickerKind,
  type Reply,
  ruleLine,
  runCommand,
  scheduledLine,
  watchLine,
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
  { label: "Memory", description: "what Enzo remembers about you", value: "/memory" },
  { label: "Forget a memory", description: "remove something Enzo remembers", value: "/forget" },
  { label: "Privacy rules", description: "what Enzo never shares", value: "/privacy" },
  { label: "Lift a privacy rule", description: "asks first", value: "/privacy lift" },
  { label: "Goals", description: "what you are working toward", value: "/goals" },
  { label: "Finished goals", description: "done or given up", value: "/goals done" },
  { label: "Scheduled", description: "reminders and tasks still to come", value: "/scheduled" },
  { label: "Cancel a reminder, task or watch", description: "asks first", value: "/cancel" },
  { label: "Watches", description: "what Enzo watches for a change", value: "/watches" },
  {
    label: "Turn on a watch",
    description: "shows what it reads now, asks first",
    value: "/enable",
  },
  { label: "Helpers", description: "long work handed to helpers", value: "/helpers" },
  { label: "Stop a helper", value: "/helpers stop" },
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
  memory: "Forget which memory?",
  forget: "Forget this?",
  reminders: "Cancel which one?",
  cancel: "Cancel this?",
  rules: "Lift which privacy rule?",
  lift: "Lift this rule?",
  helpers: "Your helpers: open one's chat",
  stophelper: "Stop which helper?",
  watches: "Turn on which watch?",
  enable: "Turn this watch on?",
};

/** Waits before opening the live event stream again: from this, doubling, up to the max. */
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30_000;

/** Wait, unless the app closes first. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

/** Shown while Enzo drives the browser for this chat (ADR 0027): where, and how to stop it. */
export function BrowserSign({ site }: { site: string }) {
  return (
    <box paddingLeft={1} marginTop={1} flexShrink={0}>
      <text>
        <span fg={COLOR.warn}>● {browserSign(site)}</span>
        <span fg={COLOR.dim}> · esc stops it</span>
      </text>
    </box>
  );
}

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
  client: ChatClient &
    AccountsClient &
    ChatsClient &
    MemoryClient &
    ScheduleClient &
    GoalsClient &
    PrivacyClient &
    HelpersClient;
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
  // `google`: the prompt takes the user's Google OAuth client file, then signs in with that access.
  const [login, setLogin] = useState<{
    provider: string;
    card: CardHandle;
    google?: GoogleAccess;
  } | null>(null);
  const [picker, setPicker] = useState<PickerKind | "palette" | null>(null);
  const [sidebar, setSidebar] = useState<boolean | null>(null); // null: by terminal width
  const [details, setDetails] = useState(true);
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  // Past chats: the newest one for the opening screen's "continue" line, all of them for /chats.
  const [chats, setChats] = useState<ChatSummary[]>([]);
  // /forget: what Enzo remembers, and the one the user is being asked about.
  const [memories, setMemories] = useState<Remembered[]>([]);
  const [forgetting, setForgetting] = useState<Remembered | null>(null);
  // /cancel: the reminders to come, and the one the user is being asked about.
  const [reminders, setReminders] = useState<Scheduled[]>([]);
  const [cancelling, setCancelling] = useState<Scheduled | null>(null);
  // /privacy lift: the rules that hold here, and the one the user is being asked about.
  const [rules, setRules] = useState<PrivacyRule[]>([]);
  const [lifting, setLifting] = useState<PrivacyRule | null>(null);
  // /helpers: the helpers to open or stop.
  const [helpers, setHelpers] = useState<Helper[]>([]);
  // Watches: the list to pick from, the one being cancelled, and the check shown before /enable.
  const [watchList, setWatchList] = useState<Watch[]>([]);
  const [unwatching, setUnwatching] = useState<Watch | null>(null);
  const [enabling, setEnabling] = useState<WatchCheck | null>(null);
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

  // Reminders arrive while the app is open (spec §9.3): each shows in the chat on screen. The
  // stream ends when the gateway goes away; it is opened again, waiting longer each time.
  const { reminded, refresh } = chat;
  useEffect(() => {
    const stop = new AbortController();
    void (async () => {
      let wait = RETRY_MIN_MS;
      while (!stop.signal.aborted) {
        try {
          // Each time the stream opens again (Enzo restarted), the chat on screen catches up on
          // what it missed meanwhile: a helper that picked up again, or finished.
          for await (const event of client.events(stop.signal, () => void refresh())) {
            wait = RETRY_MIN_MS;
            reminded(event);
          }
        } catch {
          // the gateway is down or restarting: try again below
        }
        await pause(wait, stop.signal);
        wait = Math.min(wait * 2, RETRY_MAX_MS);
      }
    })();
    return () => stop.abort();
  }, [client, reminded, refresh]);

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

  /** Sign in with Google; with no OAuth client yet, the hidden prompt asks for the user's own. */
  const googleSignIn = async (access: GoogleAccess, card: CardHandle): Promise<Reply> => {
    card.show("Opening your browser to sign in with Google…");
    const result = await signInWithGoogle(client, {
      access,
      open: openUrl,
      // The link gets its own line: a terminal must never wrap it into other words.
      waiting: (url) =>
        card.show(
          `Finish signing in with Google in your browser. If no page opened, go to:\n${url}`,
        ),
    });
    if (result.needsClient) {
      setLogin({ provider: "google", card, google: access });
      return result.text;
    }
    if (!result.ok) return { text: result.text, error: true };
    accountsChanged();
    return result.text;
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
    signInWithGoogle: (access) => googleSignIn(access, card),
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
    pickMemory: async () => {
      const { memories: all } = await client.memories();
      if (all.length === 0) return "Enzo remembers nothing yet, so there is nothing to forget.";
      setMemories(all);
      setPicker("memory");
      return null;
    },
    askForget: async (id) => {
      const known = memories.find((m) => m.id === id);
      const found = known ?? (await client.memories()).memories.find((m) => m.id === id);
      if (!found) return { text: `Enzo has no memory #${id}. /memory lists them.`, error: true };
      setForgetting(found);
      setPicker("forget");
      return null;
    },
    forgetMemory: async () => {
      const target = forgetting;
      if (!target) return null;
      setForgetting(null);
      await client.forgetMemory(target.id);
      return `Forgot “${memoryLine(target)}”.`;
    },
    keepMemory: () => setForgetting(null),
    pickRule: async () => {
      const all = await client.privacyRules(state.conversationId);
      if (all.length === 0) return "No privacy rules to lift.";
      setRules(all);
      setPicker("rules");
      return null;
    },
    askLift: async (id) => {
      const found =
        rules.find((r) => r.id === id) ??
        (await client.privacyRules(state.conversationId)).find((r) => r.id === id);
      if (!found) return { text: `There is no rule #${id}. /privacy lists them.`, error: true };
      setLifting(found);
      setPicker("lift");
      return null;
    },
    liftRule: async () => {
      const target = lifting;
      if (!target) return null;
      setLifting(null);
      await client.removePrivacyRule(target.id);
      return `Lifted the rule “${target.text}”.`;
    },
    keepRule: () => setLifting(null),
    pickReminder: async () => {
      const [all, watches] = await Promise.all([client.scheduled(), client.watches()]);
      if (all.length === 0 && watches.length === 0) return "Nothing scheduled to cancel.";
      setReminders(all);
      setWatchList(watches);
      setPicker("reminders");
      return null;
    },
    askCancelWatch: async (id) => {
      const found = (await client.watches()).find((w) => w.id === id);
      if (!found) {
        return { text: `There is no watch #${id}. /watches lists them.`, error: true };
      }
      setCancelling(null);
      setUnwatching(found);
      setPicker("cancel");
      return null;
    },
    pickWatch: async () => {
      const off = (await client.watches()).filter((w) => w.status !== "on");
      if (off.length === 0) {
        return "No watch is off. Ask Enzo to watch something, like “tell me when the kettle is under $40”.";
      }
      setWatchList(off);
      setPicker("watches");
      return null;
    },
    askEnable: async (id) => {
      const check = await client.checkWatch(id); // the dry run: what it reads right now
      setEnabling(check);
      setPicker("enable");
      return checkReply(check);
    },
    enableWatch: async () => {
      const check = enabling;
      if (!check) return null;
      setEnabling(null);
      const on = await client.enableWatch(check.watch.id, check.token);
      return `Watch #${on.id} is on: ${on.checks}. Unchanged checks stay silent; a change wakes Enzo.`;
    },
    keepWatchOff: () => setEnabling(null),
    askCancel: async (id) => {
      const found =
        reminders.find((r) => r.id === id) ?? (await client.scheduled()).find((r) => r.id === id);
      if (!found)
        return {
          text: `There is nothing scheduled with #${id}. /scheduled lists them.`,
          error: true,
        };
      setUnwatching(null);
      setCancelling(found);
      setPicker("cancel");
      return null;
    },
    cancelReminder: async () => {
      const watch = unwatching;
      if (watch) {
        setUnwatching(null);
        await client.cancelWatch(watch.id);
        return `Stopped watching “${watch.what}”.`;
      }
      const target = cancelling;
      if (!target) return null;
      setCancelling(null);
      await client.cancelScheduled(target.id);
      return `Cancelled “${target.text}”.`;
    },
    keepReminder: () => {
      setCancelling(null);
      setUnwatching(null);
    },
    pickHelper: async (stopping) => {
      const all = await client.helpers();
      const shown = stopping
        ? all.filter((h) => h.status === "working" || h.status === "waiting")
        : all;
      if (shown.length === 0) {
        return stopping
          ? "No helper is working now."
          : "No helpers yet. Ask Enzo for long work, like “research these 5 laptops and compare them”: a helper does it while you keep talking.";
      }
      setHelpers(shown);
      setPicker(stopping ? "stophelper" : "helpers");
      return null;
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

  /** The user's own Google OAuth client: kept in the vault, then the sign-in goes on. */
  const saveGoogleClient = (file: string, access: GoogleAccess, card: CardHandle) => {
    setLogin(null);
    card.show("Saving your Google OAuth client…");
    void (async () => {
      try {
        await client.setGoogleClient(file);
      } catch (e: unknown) {
        card.show({ text: e instanceof Error ? e.message : String(e), error: true });
        return;
      }
      const reply = await googleSignIn(access, card);
      if (reply !== null) card.show(reply);
    })();
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
      // Esc also closes a browser task that waits for an answer, when no reply is running.
      const waiting = now.state.browser !== null && now.state.status !== "streaming";
      const what = waiting ? "Closed the browser" : "Stopping the reply…";
      void now.stop().then((stopping) => {
        if (stopping) say(what);
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
    if (kind === "memory") {
      // The memory itself gets the whole width; /memory shows where each came from.
      return memories.map((m) => ({ label: memoryLine(m), value: `/forget ${m.id}` }));
    }
    if (kind === "rules") {
      return rules.map((r) => ({ label: ruleLine(r), value: `/privacy ${r.id}` }));
    }
    if (kind === "lift") {
      // Keeping it is first: a stray Enter never lifts a rule.
      const what = lifting ? lifting.text : "";
      return [
        { label: "No, keep it", value: "/privacy keep" },
        {
          label: `Yes, lift “${what.length > 40 ? `${what.slice(0, 39)}…` : what}”`,
          value: "/privacy confirm",
        },
      ];
    }
    if (kind === "helpers" || kind === "stophelper") {
      return helpers.map((h) => ({
        label: `#${h.id} ${h.title}`,
        description: helperLine(h),
        value: kind === "stophelper" ? `/stophelper ${h.id}` : `/open ${h.chat_id ?? ""}`,
        ...(h.status === "working" || h.status === "waiting" ? {} : { muted: true }),
      }));
    }
    if (kind === "reminders") {
      return [
        ...reminders.map((r) => ({ label: scheduledLine(r), value: `/cancel ${r.id}` })),
        ...watchList.map((w) => ({ label: `Watch: ${w.what}`, value: `/cancel w${w.id}` })),
      ];
    }
    if (kind === "watches") {
      return watchList.map((w) => ({ label: watchLine(w), value: `/enable ${w.id}` }));
    }
    if (kind === "enable") {
      // Keeping it off is first: a stray Enter never turns anything on. The dry run is on the
      // choice itself: the list covers the card that showed it (spec §9.4: inspect, then enable).
      const reads = enabling ? enabling.reads : "";
      return [
        { label: "No, keep it off", value: "/enable keep" },
        {
          label: `Yes, turn it on: it reads “${reads.length > 40 ? `${reads.slice(0, 39)}…` : reads}” now`,
          value: "/enable confirm",
        },
      ];
    }
    if (kind === "cancel") {
      // Keeping it is first: a stray Enter never cancels anything.
      const what = unwatching ? `watching ${unwatching.what}` : cancelling ? cancelling.text : "";
      return [
        { label: "No, keep it", value: "/cancel keep" },
        {
          label: `Yes, cancel “${what.length > 40 ? `${what.slice(0, 39)}…` : what}”`,
          value: "/cancel confirm",
        },
      ];
    }
    if (kind === "forget") {
      // Keeping it is first: a stray Enter never forgets anything.
      const what = forgetting ? memoryLine(forgetting) : "";
      return [
        { label: "No, keep it", value: "/forget keep" },
        {
          label: `Yes, forget “${what.length > 40 ? `${what.slice(0, 39)}…` : what}”`,
          value: "/forget confirm",
        },
      ];
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
      {...(login.google ? { what: "Google OAuth client file" } : {})}
      onSubmit={(key) => {
        const access = login.google;
        if (access) saveGoogleClient(key, access, login.card);
        else saveKey(login.provider, key, login.card);
      }}
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
              {state.browser ? <BrowserSign site={state.browser} /> : null}
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
