import {
  type AccountsClient,
  CHATGPT_USAGE_URL,
  type ChatClient,
  describeCatalog,
  describeChoice,
  describeConnection,
  resolveChoice,
} from "@enzo/chat";

/** What a command shows: plain text, or text marked as an error. `null` shows nothing. */
export type Reply = string | { text: string; error: true } | null;

export interface CommandContext {
  client: ChatClient & AccountsClient;
  /** The conversation's current choice: a model, a tier, or null for smart routing. */
  current: string | null;
  chooseBrain(choice: string | null): Promise<void>;
  /** Start a new conversation; the current one stays saved. */
  newChat(): Promise<void>;
  /** Open the hidden key prompt for a provider. */
  startLogin(provider: string): void;
  /** Sign in with ChatGPT in the browser. Shows progress itself; resolves with the outcome. */
  signInWithChatGPT(newAccount: boolean): Promise<Reply>;
  quit(): void;
}

interface Command {
  usage: string;
  summary: string;
  /** Exactly this many arguments; anything else shows the usage (and never echoes the input). */
  args: 0 | 1;
  /** Extra arguments allowed exactly as written, e.g. `/login chatgpt new`. */
  also?: readonly string[];
  /** Shown instead of the usage when there are too many arguments. */
  tooMany?: string;
  run(ctx: CommandContext, arg: string): Reply | Promise<Reply>;
}

const error = (text: string): Reply => ({ text, error: true });

const COMMANDS: Record<string, Command> = {
  "/login": {
    usage: "/login <provider|chatgpt>",
    summary: "connect an account: an API key (hidden as you paste it), or chatgpt for your plan",
    args: 1,
    also: ["chatgpt new"],
    // A key typed on the command line is on screen and may land in history: refuse it.
    tooMany:
      "Never type a key on the command line. Run /login <provider> and paste it at the hidden prompt.",
    async run(ctx, provider) {
      if (provider === "chatgpt" || provider === "chatgpt new") {
        return ctx.signInWithChatGPT(provider === "chatgpt new");
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
    usage: "/logout <provider>",
    summary: "disconnect: deletes the key, or signs out of ChatGPT",
    args: 1,
    async run(ctx, provider) {
      const out = await ctx.client.disconnect(provider);
      if (out.remote_sign_out === "confirmed") return `Signed out of ChatGPT (${provider}).`;
      if (out.remote_sign_out === "not_confirmed") {
        return `Signed out here, but OpenAI did not confirm it. To be sure, disconnect Enzo in your ChatGPT settings (${CHATGPT_USAGE_URL}).`;
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
    summary: "test a saved key or sign-in with the provider",
    args: 1,
    async run(ctx, provider) {
      const r = await ctx.client.checkConnection(provider);
      return r.ok ? `${provider}: the connection works.` : error(`${provider}: ${r.detail}`);
    },
  },
  "/models": {
    usage: "/models",
    summary: "models by tier: fast, balanced, powerful (★ recommended)",
    args: 0,
    async run(ctx) {
      return describeCatalog(await ctx.client.brains(), ctx.current);
    },
  },
  "/model": {
    usage: "/model <name|tier|auto>",
    summary: "use one model, any model of a tier, or smart routing",
    args: 1,
    async run(ctx, arg) {
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
      return "New chat started.";
    },
  },
  "/help": {
    usage: "/help",
    summary: "this list",
    args: 0,
    run: () => HELP,
  },
  "/quit": {
    usage: "/quit",
    summary: "exit",
    args: 0,
    run(ctx) {
      ctx.quit();
      return null;
    },
  },
};

export const HELP = Object.values(COMMANDS)
  .map((c) => `${c.usage.padEnd(26)} ${c.summary}`)
  .join("\n");

/** Run a `/command` line. Failures come back as error replies, never as thrown exceptions. */
export async function runCommand(line: string, ctx: CommandContext): Promise<Reply> {
  const [name = "", ...args] = line.trim().split(/\s+/);
  const command = COMMANDS[name];
  if (!command) return error(`Unknown command: ${name}. Commands:\n${HELP}`);
  const joined = args.join(" ");
  const allowed = command.also?.includes(joined) ?? false;
  if (!allowed && args.length > command.args && command.tooMany) return error(command.tooMany);
  if (!allowed && args.length !== command.args) return error(`Usage: ${command.usage}`);
  try {
    return await command.run(ctx, allowed ? joined : (args[0] ?? ""));
  } catch (e: unknown) {
    return error(e instanceof Error ? e.message : String(e));
  }
}
