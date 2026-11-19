import {
  type AccountsClient,
  type ChatClient,
  type ConversationMemory,
  describeApproval,
  formatCost,
  signInWithChatGPT,
  useChat,
} from "@enzo/chat";
import { useKeyboard } from "@opentui/react";
import { useRef, useState } from "react";
import { openInBrowser } from "./browser.ts";
import { type CommandContext, type Reply, runCommand } from "./commands.ts";
import { KeyPrompt } from "./KeyPrompt.tsx";

const DIM = "#8a8a8a";

export function App({
  client,
  memory,
  onQuit,
  openUrl = openInBrowser,
}: {
  client: ChatClient & AccountsClient;
  memory: ConversationMemory;
  onQuit: () => void;
  /** Opens sign-in pages; tests pass a fake. */
  openUrl?: (url: string) => Promise<void>;
}) {
  const { state, send, decide, chooseBrain, newChat } = useChat(client, memory, "cli");
  const [reply, setReply] = useState<Reply>(null);
  // Provider whose key is being entered at the hidden prompt.
  const [login, setLogin] = useState<string | null>(null);
  // Remounting the input is how it clears after a submit (its `value` prop only seeds it).
  const [inputKey, setInputKey] = useState(0);
  const approval = state.approvals[0];
  const typing = state.status === "ready" && !approval;

  const chatGPT = async (newAccount: boolean): Promise<Reply> => {
    setReply("Opening your browser to sign in with ChatGPT…");
    const result = await signInWithChatGPT(client, {
      newAccount,
      open: openUrl,
      waiting: (url) =>
        // The link gets its own line: a terminal must never wrap it, or a click opens half of it.
        setReply(
          `Finish signing in with ChatGPT in your browser. If no page opened, go to:\n${url}`,
        ),
    });
    if (!result.ok) return { text: result.text, error: true };
    return result.usageUrl ? `${result.text} Manage usage: ${result.usageUrl}` : result.text;
  };

  const context: CommandContext = {
    client,
    current: state.brain,
    chooseBrain,
    newChat,
    startLogin: setLogin,
    signInWithChatGPT: chatGPT,
    quit: onQuit,
  };

  const saveKey = (provider: string, key: string) => {
    setLogin(null);
    setReply(`Checking your key with ${provider}…`);
    void (async () => {
      try {
        await client.connect(provider, key);
        setReply(`Connected ${provider}. The key is stored in the vault, never in chats or logs.`);
      } catch (e: unknown) {
        setReply({ text: e instanceof Error ? e.message : String(e), error: true });
      }
    })();
  };

  // y / n answer the approval on screen; nothing else does. Read through a ref so the handler
  // always sees the latest state, whenever the renderer delivers the key.
  const latest = useRef({ approval, ready: state.status === "ready", decide });
  latest.current = { approval, ready: state.status === "ready", decide };
  useKeyboard((key) => {
    const now = latest.current;
    if (!now.approval || !now.ready) return;
    if (key.name === "y") void now.decide(now.approval.id, true);
    if (key.name === "n") void now.decide(now.approval.id, false);
  });

  const submit = (value: string) => {
    setInputKey((k) => k + 1);
    setReply(null);
    const line = value.trim();
    if (!line) return;
    if (line.startsWith("/")) return void runCommand(line, context).then(setReply);
    void send(line);
  };

  if (state.status === "connecting") return <text fg={DIM}>Connecting to Enzo…</text>;
  if (state.status === "offline") {
    return <text fg="red">Gateway unreachable: {state.error}</text>;
  }

  const shown = typeof reply === "string" ? { text: reply, error: false } : reply;
  return (
    <box flexDirection="column">
      <text>
        <strong>Enzo</strong>
        <span fg={DIM}> · model: {state.brain ?? "auto"} · /help for commands</span>
      </text>
      {state.items.length === 0 && !state.draft ? <Welcome brains={state.brains} /> : null}
      {state.items.map((item, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
        <text key={i} {...(item.role === "notice" ? { fg: DIM } : {})}>
          {item.role === "user" ? <span fg="cyan">you: </span> : null}
          {item.role === "assistant" ? <span fg="green">enzo: </span> : null}
          {item.text}
          {item.by ? <span fg={DIM}> · {item.by}</span> : null}
        </text>
      ))}
      {state.draft ? (
        <text>
          <span fg="green">enzo: </span>
          {state.draft}
        </text>
      ) : null}
      {state.working > 0 ? <text fg={DIM}>Working…</text> : null}
      {approval ? (
        <box
          border
          borderColor="yellow"
          title="Approve this action? (y / n)"
          flexDirection="column"
        >
          <text>
            {describeApproval(approval)}
            {"\n"}Exact details: {JSON.stringify(approval.arguments, null, 2)}
            {approval.detail ? `\n${clip(approval.detail, 12)}` : ""}
          </text>
        </box>
      ) : null}
      {state.error ? <text fg="red">{state.error}</text> : null}
      {shown ? <text fg={shown.error ? "red" : "yellow"}>{shown.text}</text> : null}
      {login ? (
        <KeyPrompt
          provider={login}
          onSubmit={(key) => saveKey(login, key)}
          onCancel={() => {
            setLogin(null);
            setReply("Login cancelled. Nothing was saved.");
          }}
        />
      ) : (
        <box
          border
          title={state.status === "streaming" ? "Enzo is working…" : "Message Enzo"}
          height={3}
        >
          <input key={inputKey} focused={typing} placeholder="Type a message" onSubmit={submit} />
        </box>
      )}
      {state.lastCost !== undefined ? (
        <text fg={DIM}>
          {formatCost(state.lastCost, state.lastPlan)}
          {state.lastPlan ? " · manage usage: chatgpt.com/settings/usage" : ""}
        </text>
      ) : null}
    </box>
  );
}

/** What a new chat shows: how to start, and how to connect when no model account is. */
function Welcome({ brains }: { brains: { available: boolean }[] }) {
  const connected = brains.some((b) => b.available);
  return (
    <box flexDirection="column" paddingTop={1} paddingBottom={1}>
      <text>Hi, I'm Enzo, your personal agent. Type a message to start.</text>
      {connected ? (
        <text fg={DIM}>/model picks a model · /help lists every command</text>
      ) : (
        // Short lines: a command a user must type is never wrapped in half.
        <box flexDirection="column">
          <text fg="yellow">No model account is connected yet. Connect one:</text>
          <text fg="yellow">· your ChatGPT plan, no key needed: /login chatgpt</text>
          <text fg="yellow">· an API key: /login anthropic, openai, xai or moonshot</text>
        </box>
      )}
    </box>
  );
}

/** The first lines of a long detail (a diff): the exact action above it stays on screen. */
function clip(text: string, lines: number): string {
  const all = text.split("\n");
  return all.length <= lines
    ? text
    : `${all.slice(0, lines).join("\n")}\n… ${all.length - lines} more lines`;
}
