import { type ChatClient, type ConversationMemory, formatCost, useChat } from "@enzo/chat";
import { useKeyboard } from "@opentui/react";
import { useRef, useState } from "react";

const HELP = "Commands: /brains, /brain <name|auto>, /quit";
const DIM = "#8a8a8a";

export function App({
  client,
  memory,
  onQuit,
}: {
  client: ChatClient;
  memory: ConversationMemory;
  onQuit: () => void;
}) {
  const { state, send, decide, chooseBrain } = useChat(client, memory, "cli");
  const [info, setInfo] = useState<string | null>(null);
  // Remounting the input is how it clears after a submit (its `value` prop only seeds it).
  const [inputKey, setInputKey] = useState(0);
  const approval = state.approvals[0];
  const typing = state.status === "ready" && !approval;

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
        return setInfo(`Unknown brain: ${brain}. ${HELP}`);
      }
      return void chooseBrain(brain).then(() => setInfo(`Brain: ${brain ?? "auto (routing)"}`));
    }
    if (line.startsWith("/")) return setInfo(HELP);
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
        <span fg={DIM}>
          {" "}
          · brain: {state.brain ?? "auto"} · {HELP}
        </span>
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
      {info ? <text fg="yellow">{info}</text> : null}
      <box
        border
        title={state.status === "streaming" ? "Enzo is working…" : "Message Enzo"}
        height={3}
      >
        <input key={inputKey} focused={typing} placeholder="Type a message" onSubmit={submit} />
      </box>
      {state.lastCost !== undefined ? <text fg={DIM}>{formatCost(state.lastCost)}</text> : null}
    </box>
  );
}
