import {
  type AccountsClient,
  type ChatClient,
  type ConversationMemory,
  describeConnection,
  formatCost,
  parseLogin,
  useChat,
} from "@enzo/chat";
import { useKeyboard } from "@opentui/react";
import { useRef, useState } from "react";
import { KeyPrompt } from "./KeyPrompt.tsx";

const HELP = [
  "/login <provider>   connect your own account (key is hidden as you paste it)",
  "/logout <provider>  disconnect and delete the key",
  "/connections        providers, connection state and models",
  "/check <provider>   test a saved key with the provider",
  "/brains             list brains · /brain <name|auto> choose one",
  "/quit               exit",
].join("\n");
const DIM = "#8a8a8a";

interface Info {
  text: string;
  error?: boolean;
}

const failure = (e: unknown) => ({ text: e instanceof Error ? e.message : String(e), error: true });

export function App({
  client,
  memory,
  onQuit,
}: {
  client: ChatClient & AccountsClient;
  memory: ConversationMemory;
  onQuit: () => void;
}) {
  const { state, send, decide, chooseBrain } = useChat(client, memory, "cli");
  const [info, setInfoState] = useState<Info | null>(null);
  const setInfo = (text: string | null) => setInfoState(text === null ? null : { text });
  // Provider whose key is being entered at the hidden prompt.
  const [login, setLogin] = useState<string | null>(null);
  // Remounting the input is how it clears after a submit (its `value` prop only seeds it).
  const [inputKey, setInputKey] = useState(0);
  const approval = state.approvals[0];
  const typing = state.status === "ready" && !approval;

  const command = async (run: () => Promise<Info | string>) => {
    try {
      const out = await run();
      setInfoState(typeof out === "string" ? { text: out } : out);
    } catch (e: unknown) {
      setInfoState(failure(e));
    }
  };

  const startLogin = (provider: string) =>
    command(async () => {
      const known = (await client.connections()).map((c) => c.provider);
      if (!known.includes(provider)) {
        return {
          text: `Unknown provider: ${provider}. Providers: ${known.join(", ")}`,
          error: true,
        };
      }
      setLogin(provider);
      return `Connecting ${provider}. Paste your API key below; it is never shown or saved in chats.`;
    });

  const saveKey = (provider: string, key: string) => {
    setLogin(null);
    setInfo(`Checking your key with ${provider}…`);
    void command(async () => {
      await client.connect(provider, key);
      return `Connected ${provider}. The key is stored in the vault, never in chats or logs.`;
    });
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
    setInfo(null);
    const line = value.trim();
    if (!line) return;
    if (line === "/quit") return onQuit();
    if (line === "/help") return setInfo(HELP);
    const loginLine = parseLogin(line);
    if (loginLine) {
      if ("error" in loginLine) return setInfoState({ text: loginLine.error, error: true });
      return void startLogin(loginLine.provider);
    }
    if (line === "/connections") {
      return void command(async () =>
        (await client.connections()).map(describeConnection).join("\n"),
      );
    }
    const [name, provider, extra] = line.split(/\s+/);
    if ((name === "/logout" || name === "/check") && (!provider || extra)) {
      return setInfoState({ text: `Usage: ${name} <provider>`, error: true });
    }
    if (name === "/logout" && provider) {
      return void command(async () => {
        await client.disconnect(provider);
        return `Disconnected ${provider}. Its key was deleted.`;
      });
    }
    if (name === "/check" && provider) {
      return void command(async () => {
        const r = await client.checkConnection(provider);
        return r.ok
          ? `${provider}: the key works.`
          : { text: `${provider}: ${r.detail}`, error: true };
      });
    }
    if (line === "/brains") {
      return setInfo(
        state.brains
          .map((b) => `${b.name} (${b.model})${b.priced ? "" : " cost unknown"}`)
          .join("\n"),
      );
    }
    if (line.startsWith("/brain ")) {
      const name = line.slice("/brain ".length).trim();
      const brain = name === "auto" ? null : name;
      if (brain && !state.brains.some((b) => b.name === brain)) {
        return setInfo(`Unknown brain: ${brain}. Try /brains.`);
      }
      return void chooseBrain(brain).then(() => setInfo(`Brain: ${brain ?? "auto (routing)"}`));
    }
    if (line.startsWith("/")) return setInfo(`Unknown command. Commands:\n${HELP}`);
    void send(line);
  };

  if (state.status === "connecting") return <text fg={DIM}>Connecting to Enzo…</text>;
  if (state.status === "offline") {
    return <text fg="red">Gateway unreachable: {state.error}</text>;
  }

  return (
    <box flexDirection="column">
      <text>
        <strong>Enzo</strong>
        <span fg={DIM}> · brain: {state.brain ?? "auto"} · /help for commands</span>
      </text>
      {state.items.map((item, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
        <text key={i} {...(item.role === "notice" ? { fg: DIM } : {})}>
          {item.role === "user" ? <span fg="cyan">you: </span> : null}
          {item.role === "assistant" ? <span fg="green">enzo: </span> : null}
          {item.text}
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
            {approval.tool} with exactly: {JSON.stringify(approval.arguments, null, 2)}
          </text>
        </box>
      ) : null}
      {state.error ? <text fg="red">{state.error}</text> : null}
      {info ? <text fg={info.error ? "red" : "yellow"}>{info.text}</text> : null}
      {login ? (
        <KeyPrompt
          provider={login}
          onSubmit={(key) => saveKey(login, key)}
          onCancel={() => {
            setLogin(null);
            setInfo("Login cancelled. Nothing was saved.");
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
      {state.lastCost !== undefined ? <text fg={DIM}>{formatCost(state.lastCost)}</text> : null}
    </box>
  );
}
