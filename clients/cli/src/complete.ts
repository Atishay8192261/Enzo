import { methodName, TIER_HINT } from "@enzo/chat";
import { type Brain, type ProviderConnection, TIERS } from "@enzo/sdk";
import { COMMAND_LIST } from "./commands.ts";

/** One line of the slash-command popup (#34). */
export interface Suggestion {
  /** What the input becomes when it is chosen. */
  value: string;
  label: string;
  description: string;
  /** Choosing it runs it at once: nothing is left to type. */
  complete: boolean;
}

export interface Sources {
  connections: ProviderConnection[];
  brains: Brain[];
}

/** What the popup lists for the input so far: commands, then each command's arguments. */
export function suggest(input: string, { connections, brains }: Sources): Suggestion[] {
  if (!input.startsWith("/") || input.includes("\n")) return [];
  const space = input.indexOf(" ");
  if (space < 0) {
    const typed = input.toLowerCase();
    return COMMAND_LIST.filter((c) => c.name.startsWith(typed)).map((c) => ({
      value: c.takesArgument ? `${c.name} ` : c.name,
      label: c.usage,
      description: c.summary,
      complete: !c.takesArgument,
    }));
  }
  const command = input.slice(0, space);
  const typed = input
    .slice(space + 1)
    .trimStart()
    .toLowerCase();
  if (typed.includes(" ")) return []; // past the argument
  return argumentsFor(command, connections, brains)
    .filter(([arg]) => arg.toLowerCase().startsWith(typed))
    .map(([arg, description]) => ({
      value: `${command} ${arg}`,
      label: arg,
      description,
      complete: true,
    }));
}

function argumentsFor(
  command: string,
  connections: ProviderConnection[],
  brains: Brain[],
): [string, string][] {
  const connected = connections.filter((c) => c.connected);
  switch (command) {
    case "/login":
      return [
        ["chatgpt", "your ChatGPT plan pays, no key needed"],
        ...connections.map((c): [string, string] => [c.provider, loginNote(c)]),
      ];
    case "/logout":
    case "/check":
      return connected.map((c) => [c.provider, accountName(c)]);
    case "/model":
      return [
        ["auto", "smart routing across your accounts"],
        ...TIERS.map((t): [string, string] => [t, `any ${t} model: ${TIER_HINT[t]}`]),
        ...brains.map((b): [string, string] => [b.name, modelNote(b)]),
      ];
    case "/copy":
      return [["code", "only the last code block"]];
    case "/details":
      return [
        ["on", "times, tokens and cost under each message"],
        ["off", "only the words"],
      ];
    default:
      return [];
  }
}

function loginNote(c: ProviderConnection): string {
  const what = c.models.length > 0 ? "API key" : (c.purpose ?? "API key");
  return c.connected ? `connected · ${accountName(c)}` : what;
}

/** An account as people know it: "ChatGPT plan (ada@example.com)", "anthropic · API key". */
export function accountName(c: ProviderConnection): string {
  if (c.method === "chatgpt") return `ChatGPT plan${c.label ? ` (${c.label})` : ""}`;
  return `${c.provider} · ${methodName(c.method)}`;
}

function modelNote(b: Brain): string {
  const star = b.recommended ? "★ " : "";
  return b.available
    ? `${star}${b.tier} · ${b.model}`
    : `${star}${b.tier} · needs /login ${b.provider}`;
}
