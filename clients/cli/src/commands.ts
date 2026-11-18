import {
  type AccountsClient,
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
  /** Open the hidden key prompt for a provider. */
  startLogin(provider: string): void;
  quit(): void;
}

interface Command {
  usage: string;
  summary: string;
  /** Exactly this many arguments; anything else shows the usage (and never echoes the input). */
  args: 0 | 1;
  /** Shown instead of the usage when there are too many arguments. */
  tooMany?: string;
  run(ctx: CommandContext, arg: string): Reply | Promise<Reply>;
}

const error = (text: string): Reply => ({ text, error: true });

const COMMANDS: Record<string, Command> = {
  "/login": {
    usage: "/login <provider>",
    summary: "connect your own account (the key is hidden as you paste it)",
    args: 1,
    // A key typed on the command line is on screen and may land in history: refuse it.
    tooMany:
      "Never type a key on the command line. Run /login <provider> and paste it at the hidden prompt.",
    async run(ctx, provider) {
      const known = (await ctx.client.connections()).map((c) => c.provider);
      if (!known.includes(provider)) {
        return error(`Unknown provider: ${provider}. Providers: ${known.join(", ")}`);
      }
      ctx.startLogin(provider);
      return `Connecting ${provider}. Paste your API key below; it is never shown or saved in chats.`;
    },
  },
  "/logout": {
    usage: "/logout <provider>",
    summary: "disconnect and delete the key",
    args: 1,
    async run(ctx, provider) {
      await ctx.client.disconnect(provider);
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
    summary: "test a saved key with the provider",
    args: 1,
    async run(ctx, provider) {
      const r = await ctx.client.checkConnection(provider);
      return r.ok ? `${provider}: the key works.` : error(`${provider}: ${r.detail}`);
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
  .map((c) => `${c.usage.padEnd(24)} ${c.summary}`)
  .join("\n");

/** Run a `/command` line. Failures come back as error replies, never as thrown exceptions. */
export async function runCommand(line: string, ctx: CommandContext): Promise<Reply> {
  const [name = "", ...args] = line.trim().split(/\s+/);
  const command = COMMANDS[name];
  if (!command) return error(`Unknown command: ${name}. Commands:\n${HELP}`);
  if (args.length > command.args && command.tooMany) return error(command.tooMany);
  if (args.length !== command.args) return error(`Usage: ${command.usage}`);
  try {
    return await command.run(ctx, args[0] ?? "");
  } catch (e: unknown) {
    return error(e instanceof Error ? e.message : String(e));
  }
}
