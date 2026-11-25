import {
  type AccountsClient,
  CHATGPT_USAGE_URL,
  type ChatClient,
  describeCatalog,
  describeChoice,
  describeConnection,
  GOOGLE_PERMISSIONS_URL,
  type MemoryClient,
  resolveChoice,
  type ScheduleClient,
} from "@enzo/chat";
import type { GoogleAccess, MemoryList, Remembered, Scheduled } from "@enzo/sdk";

/**
 * What a command shows, as a card under its own echo: plain text, text marked as an error, or
 * two columns (a wrapped description stays under its own column, #36). `null` shows nothing.
 */
export type Reply = string | { text: string; error: true } | { rows: [string, string][] } | null;

/** The pickers a command can open instead of asking for an argument. */
export type PickerKind =
  | "login"
  | "logout"
  | "model"
  | "chats"
  | "delete"
  | "memory"
  | "forget"
  | "reminders"
  | "cancel";

export interface CommandContext {
  client: ChatClient & AccountsClient & MemoryClient & ScheduleClient;
  /** The conversation's current choice: a model, a tier, or null for smart routing. */
  current: string | null;
  /** This chat's id; null while it is fresh (created by its first message). */
  chatId: string | null;
  chooseBrain(choice: string | null): Promise<void>;
  /** Start a new conversation; the current one stays saved. */
  newChat(): Promise<void>;
  /** Open the hidden key prompt for a provider. */
  startLogin(provider: string): void;
  /** Sign in with ChatGPT in the browser. Shows progress itself; resolves with the outcome. */
  signInWithChatGPT(newAccount: boolean): Promise<Reply>;
  /** Sign in with Google (Gmail, Calendar) in the browser; asks for the user's own OAuth client
   * file first when none is set up. Shows progress itself; resolves with the outcome. */
  signInWithGoogle(access: GoogleAccess): Promise<Reply>;
  /** Open a picker (Up/Down and Enter) instead of typing the argument. */
  pick(kind: PickerKind): void;
  /** Copy the last reply, or its last code block, to the clipboard. */
  copy(what: "reply" | "code"): Promise<Reply>;
  toggleSidebar(): void;
  /** Show or hide times, tokens and cost under each message; null switches it (#49). */
  setDetails(on: boolean | null): boolean;
  /** Send the last message that did not go through again. False: there is none. */
  resend(): Promise<boolean>;
  /** Accounts changed (connected, disconnected): reload what depends on them. */
  accountsChanged(): void;
  /** Reopen a past chat (#38). */
  openChat(conversationId: string): Promise<void>;
  /** Rename this chat, or say why it can't be yet. */
  renameChat(title: string): Promise<Reply>;
  /** Move this chat to the trash (after the user confirmed) and start a fresh one. */
  deleteChat(): Promise<Reply>;
  /** The list of memories to forget one from, or why there is none. */
  pickMemory(): Promise<Reply>;
  /** Ask before forgetting this memory. */
  askForget(id: number): Promise<Reply>;
  /** Forget the memory the user just confirmed; null: none was being asked about. */
  forgetMemory(): Promise<Reply>;
  /** The user kept the memory. */
  keepMemory(): void;
  /** The list of reminders to cancel one from, or why there is none. */
  pickReminder(): Promise<Reply>;
  /** Ask before cancelling this reminder. */
  askCancel(id: number): Promise<Reply>;
  /** Cancel the reminder the user just confirmed; null: none was being asked about. */
  cancelReminder(): Promise<Reply>;
  /** The user kept the reminder. */
  keepReminder(): void;
  quit(): void;
}

interface Command {
  usage: string;
  summary: string;
  /** Exactly this many arguments; anything else shows the usage (and never echoes the input). */
  args: 0 | 1;
  /** With `args: 1`, no argument is fine too: the command opens a picker. */
  optional?: true;
  /** Extra arguments allowed exactly as written, e.g. `/login chatgpt new`. */
  also?: readonly string[];
  /** Shown instead of the usage when there are too many arguments. */
  tooMany?: string;
  /** Not listed in /help or the popup (another name for a listed command). */
  hidden?: true;
  /** With `args: 1`, the rest of the line is the argument (a name with spaces). */
  rest?: true;
  run(ctx: CommandContext, arg: string): Reply | Promise<Reply>;
}

const error = (text: string): Reply => ({ text, error: true });

const quit: Command = {
  usage: "/quit",
  summary: "exit Enzo (or type exit, or press ctrl+d)",
  args: 0,
  run(ctx) {
    ctx.quit();
    return null;
  },
};

const scheduledCommand: Command = {
  usage: "/scheduled",
  summary: "your reminders and scheduled tasks still to come (also /reminders, /tasks)",
  args: 0,
  async run(ctx) {
    return scheduledReply(await ctx.client.scheduled());
  },
};

const chats: Command = {
  usage: "/chats",
  summary: "your past chats: reopen one (also /resume, ctrl+r)",
  args: 0,
  run(ctx) {
    ctx.pick("chats");
    return null;
  },
};

const COMMANDS: Record<string, Command> = {
  "/login": {
    usage: "/login [provider|chatgpt]",
    summary: "your ChatGPT plan, Google, or a key (hidden)",
    args: 1,
    optional: true,
    also: ["chatgpt new", "google read"],
    // A key typed on the command line is on screen and may land in history: refuse it.
    tooMany:
      "Never type a key on the command line. Run /login <provider> and paste it at the hidden prompt.",
    async run(ctx, provider) {
      if (!provider) {
        ctx.pick("login");
        return null;
      }
      if (provider === "chatgpt" || provider === "chatgpt new") {
        return ctx.signInWithChatGPT(provider === "chatgpt new");
      }
      if (provider === "google" || provider === "google read") {
        return ctx.signInWithGoogle(provider === "google read" ? "read" : "full");
      }
      const known = (await ctx.client.connections()).map((c) => c.provider);
      if (!known.includes(provider)) {
        return error(`Unknown provider: ${provider}. Providers: ${known.join(", ")}`);
      }
      ctx.startLogin(provider);
      const plan =
        provider === "openai" ? " (or use your ChatGPT plan instead: /login chatgpt)" : "";
      return `Connecting ${provider}. Paste your API key below; it is never shown or saved in chats.${plan}`;
    },
  },
  "/logout": {
    usage: "/logout [provider]",
    summary: "disconnect: delete a key or sign out of ChatGPT",
    args: 1,
    optional: true,
    async run(ctx, provider) {
      if (!provider) {
        ctx.pick("logout");
        return null;
      }
      const out = await ctx.client.disconnect(provider);
      ctx.accountsChanged();
      if (provider === "google" && out.remote_sign_out === "confirmed") {
        return "Signed out of Google. Google removed Enzo's access to Gmail and Calendar.";
      }
      if (provider === "google" && out.remote_sign_out === "not_confirmed") {
        return `Signed out here, but Google did not confirm it. To be sure, remove Enzo in your Google Account:\n${GOOGLE_PERMISSIONS_URL}`;
      }
      if (out.remote_sign_out === "confirmed") return `Signed out of ChatGPT (${provider}).`;
      if (out.remote_sign_out === "not_confirmed") {
        return `Signed out here, but OpenAI did not confirm it. To be sure, disconnect Enzo in your ChatGPT settings:\n${CHATGPT_USAGE_URL}`;
      }
      return `Disconnected ${provider}. Its key was deleted.`;
    },
  },
  "/connections": {
    usage: "/connections",
    summary: "providers, connection state and models",
    args: 0,
    async run(ctx) {
      return (await ctx.client.connections()).map(describeConnection).join("\n");
    },
  },
  "/check": {
    usage: "/check <provider>",
    summary: "test a saved key or sign-in",
    args: 1,
    async run(ctx, provider) {
      const r = await ctx.client.checkConnection(provider);
      if (!r.ok) return error(`${provider}: ${r.detail}`);
      return `${provider}: ${r.detail ?? "the connection works."}`;
    },
  },
  "/models": {
    usage: "/models",
    summary: "models by tier: fast, balanced, powerful",
    args: 0,
    async run(ctx) {
      return describeCatalog(await ctx.client.brains(), ctx.current);
    },
  },
  "/model": {
    usage: "/model [name|tier|auto]",
    summary: "pick a model, a tier, or auto (smart routing)",
    args: 1,
    optional: true,
    async run(ctx, arg) {
      if (!arg) {
        ctx.pick("model");
        return null;
      }
      const brains = await ctx.client.brains();
      const picked = resolveChoice(arg, brains);
      if ("error" in picked) return error(picked.error);
      await ctx.chooseBrain(picked.choice);
      return `Model: ${describeChoice(picked.choice, brains)}`;
    },
  },
  "/new": {
    usage: "/new",
    summary: "start a new chat (this one stays saved)",
    args: 0,
    async run(ctx) {
      await ctx.newChat();
      return null; // the opening screen says it: a fresh chat
    },
  },
  "/retry": {
    usage: "/retry",
    summary: "send your last message again if it failed",
    args: 0,
    async run(ctx) {
      return (await ctx.resend()) ? null : "Nothing to send again: your last message went through.";
    },
  },
  "/copy": {
    usage: "/copy [code]",
    summary: "copy the last reply (or its last code block)",
    args: 0,
    also: ["code"],
    run: (ctx, what) => ctx.copy(what === "code" ? "code" : "reply"),
  },
  "/details": {
    usage: "/details [on|off]",
    summary: "show or hide times, tokens and cost",
    args: 0,
    also: ["on", "off"],
    run(ctx, arg) {
      const on = ctx.setDetails(arg === "on" ? true : arg === "off" ? false : null);
      return on
        ? "Details on: each message shows when it was sent, its model, time, tokens and cost."
        : "Details off. The status bar still shows what this chat used.";
    },
  },
  "/sidebar": {
    usage: "/sidebar",
    summary: "show or hide the side panel",
    args: 0,
    run(ctx) {
      ctx.toggleSidebar();
      return null;
    },
  },
  "/chats": chats,
  "/resume": { ...chats, usage: "/resume", hidden: true },
  "/open": {
    usage: "/open <chat>",
    summary: "reopen a chat (the /chats list uses it)",
    args: 1,
    hidden: true,
    async run(ctx, id) {
      await ctx.openChat(id);
      return null;
    },
  },
  "/rename": {
    usage: "/rename <name>",
    summary: "give this chat a name",
    args: 1,
    rest: true,
    run: (ctx, title) => ctx.renameChat(title),
  },
  "/delete": {
    usage: "/delete",
    summary: "move this chat to the trash (asks first)",
    args: 0,
    also: ["confirm", "keep"],
    async run(ctx, answer) {
      if (answer === "confirm") return ctx.deleteChat();
      if (answer === "keep") return null;
      if (!ctx.chatId) return "Nothing to delete: this chat has no messages yet.";
      ctx.pick("delete");
      return null;
    },
  },
  "/memory": {
    usage: "/memory",
    summary: "what Enzo remembers about you, and where each came from",
    args: 0,
    async run(ctx) {
      return memoryReply(await ctx.client.memories());
    },
  },
  "/forget": {
    usage: "/forget",
    summary: "remove something Enzo remembers (asks first)",
    args: 1,
    optional: true,
    also: ["keep", "confirm"],
    async run(ctx, arg) {
      if (arg === "keep") {
        ctx.keepMemory();
        return null;
      }
      if (arg === "confirm") return ctx.forgetMemory();
      if (!arg) return ctx.pickMemory();
      const id = Number(arg);
      if (!Number.isInteger(id) || id < 1) return error("Usage: /forget");
      return ctx.askForget(id);
    },
  },
  "/scheduled": scheduledCommand,
  "/reminders": { ...scheduledCommand, usage: "/reminders", hidden: true },
  "/tasks": { ...scheduledCommand, usage: "/tasks", hidden: true },
  "/cancel": {
    usage: "/cancel",
    summary: "cancel a reminder or scheduled task (asks first)",
    args: 1,
    optional: true,
    also: ["keep", "confirm"],
    async run(ctx, arg) {
      if (arg === "keep") {
        ctx.keepReminder();
        return null;
      }
      if (arg === "confirm") return ctx.cancelReminder();
      if (!arg) return ctx.pickReminder();
      const id = Number(arg);
      if (!Number.isInteger(id) || id < 1) return error("Usage: /cancel");
      return ctx.askCancel(id);
    },
  },
  "/help": {
    usage: "/help",
    summary: "commands and keys",
    args: 0,
    run: () => ({ rows: HELP_ROWS }),
  },
  "/quit": quit,
  "/exit": { ...quit, usage: "/exit", hidden: true },
};

/** Commands as the popup and /help list them. */
export const COMMAND_LIST = Object.entries(COMMANDS)
  .filter(([, c]) => !c.hidden)
  .map(([name, c]) => ({ name, usage: c.usage, summary: c.summary, takesArgument: c.args > 0 }));

export const KEYS: [string, string][] = [
  ["enter", "send"],
  ["\\ then enter", "new line (any terminal)"],
  ["shift+enter", "new line (Kitty, Ghostty, WezTerm, iTerm2)"],
  ["option+enter, ctrl+j", "new line (Terminal.app: Use Option as Meta key)"],
  ["y or n, then enter", "answer an approval card (or type a new message)"],
  ["↑ / ↓", "your earlier messages"],
  ["ctrl+r", "your past chats"],
  ["/", "commands (tab or enter completes)"],
  ["ctrl+p", "all commands"],
  ["esc", "stop the reply · close a list"],
  ["pgup / pgdn", "scroll the chat"],
  ["select with the mouse", "copies the text"],
  ["ctrl+c", "clear the message · quit when empty"],
  ["ctrl+d, or type exit", "quit"],
];

export const HELP_ROWS: [string, string][] = [
  ...COMMAND_LIST.map((c): [string, string] => [c.usage, c.summary]),
  ["", ""],
  ...KEYS,
];

/**
 * The line as a card's title. Arguments show only when the command takes them as typed: a
 * key pasted after `/login xai` must never be drawn, not even in the echo.
 */
export function echoOf(line: string): string {
  const [name = "", ...args] = line.trim().split(/\s+/);
  const command = COMMANDS[name];
  if (!command) return name.slice(0, 30);
  const joined = args.join(" ");
  // A name typed for /rename is the user's own words, shown as written (cut short).
  if (command.rest) return `${name} ${joined}`.trim().slice(0, 60);
  const fits = args.length <= command.args || (command.also?.includes(joined) ?? false);
  return fits ? [name, ...args].join(" ") : name;
}

/** Run a `/command` line. Failures come back as error replies, never as thrown exceptions. */
export async function runCommand(line: string, ctx: CommandContext): Promise<Reply> {
  const [name = "", ...args] = line.trim().split(/\s+/);
  const command = COMMANDS[name];
  if (!command) return error(`Unknown command: ${name}. Type / to see the commands.`);
  const joined = args.join(" ");
  if (command.rest) {
    if (!joined) return error(`Usage: ${command.usage}`);
    return safely(() => command.run(ctx, joined));
  }
  const allowed = command.also?.includes(joined) ?? false;
  const count = command.optional && args.length === 0 ? command.args : args.length;
  if (!allowed && count > command.args && command.tooMany) return error(command.tooMany);
  if (!allowed && count !== command.args) return error(`Usage: ${command.usage}`);
  return safely(() => command.run(ctx, allowed ? joined : (args[0] ?? "")));
}

async function safely(run: () => Reply | Promise<Reply>): Promise<Reply> {
  try {
    return await run();
  } catch (e: unknown) {
    return error(e instanceof Error ? e.message : String(e));
  }
}

/** Headings for each kind of memory, in the order /memory and Memory.md show them. */
export const MEMORY_KINDS: [Remembered["kind"], string][] = [
  ["about", "About you"],
  ["preference", "Preferences"],
  ["person", "People"],
  ["decision", "Decisions"],
  ["fact", "Other facts"],
];

/** A memory as one line: "Priya: Sister, lives in Austin." */
export const memoryLine = (m: Remembered) => (m.about ? `${m.about}: ${m.text}` : m.text);

/** What /memory shows: each memory under its kind, with where it came from (spec §15.2). */
export function memoryReply({ memories, file }: MemoryList): Reply {
  // The path on a line of its own: a terminal wraps it at a "/" otherwise (#56).
  const copy = file ? ["A copy you can read and edit by hand:", file] : [];
  if (memories.length === 0) {
    return [
      "Enzo remembers nothing yet. Tell it something, like “remember I prefer window seats”.",
      ...copy,
    ].join("\n");
  }
  const lines: string[] = [];
  for (const [kind, heading] of MEMORY_KINDS) {
    const group = memories.filter((m) => m.kind === kind);
    if (group.length === 0) continue;
    if (lines.length > 0) lines.push("");
    lines.push(heading);
    for (const m of group) lines.push(`• ${memoryLine(m)}`, `  ${m.source} · #${m.id}`);
  }
  lines.push("", "To change one, just tell Enzo. /forget removes one.", ...copy);
  return lines.join("\n");
}

/** A reminder or task as one line: "Fri 21 Nov 09:00 · Call mom". */
export const scheduledLine = (r: Scheduled) => `${r.next ?? "soon"} · ${r.text}`;

/** What /scheduled shows: reminders, then tasks, each soonest first, with its schedule. */
export function scheduledReply(all: Scheduled[]): Reply {
  if (all.length === 0) {
    return [
      "Nothing scheduled yet. Ask Enzo, like “remind me Friday at 9 to call mom” or",
      "“every weekday at 8, check the weather and tell me”.",
    ].join("\n");
  }
  const lines: string[] = [];
  for (const [kind, heading] of [
    ["reminder", "Reminders"],
    ["task", "Scheduled tasks"],
  ] as const) {
    const group = all.filter((r) => r.kind === kind);
    if (group.length === 0) continue;
    if (lines.length > 0) lines.push("");
    lines.push(heading);
    for (const r of group) {
      const stop = r.stop_when ? `, until ${r.stop_when}` : "";
      lines.push(`• ${scheduledLine(r)}`, `  ${r.schedule}${stop} · #${r.id}`);
    }
  }
  lines.push("", "To change one, just tell Enzo. /cancel removes one.");
  return lines.join("\n");
}
