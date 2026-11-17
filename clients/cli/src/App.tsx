import { type ChatClient, type ConversationMemory, formatCost, useChat } from "@enzo/chat";
import { Box, type Key, Text, useApp, useInput, usePaste } from "ink";
import { useCallback, useRef, useState } from "react";
import { LineInput } from "./LineInput.tsx";

const HELP = "Commands: /brains, /brain <name|auto>, /quit";

export function App({ client, memory }: { client: ChatClient; memory: ConversationMemory }) {
  const { state, send, decide, chooseBrain } = useChat(client, memory, "cli");
  const { exit } = useApp();
  const [text, setTextState] = useState("");
  const textRef = useRef("");
  const setText = useCallback((next: string) => {
    textRef.current = next;
    setTextState(next);
  }, []);
  const [info, setInfo] = useState<string | null>(null);
  const approval = state.approvals[0];
  const typing = state.status === "ready" && !approval;

  const submit = (value: string) => {
    const line = value.trim();
    setInfo(null);
    if (!line) return;
    if (line === "/quit") return exit();
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
      if (brain && !state.brains.some((b) => b.name === brain))
        return setInfo(`Unknown brain: ${brain}. ${HELP}`);
      return void chooseBrain(brain).then(() => setInfo(`Brain: ${brain ?? "auto (routing)"}`));
    }
    if (line.startsWith("/")) return setInfo(HELP);
    void send(line);
  };

  // One listener for the whole app, subscribed once and reading the latest state through a ref.
  // Toggling listeners per render drops keys typed between a render and its effect.
  const latest = useRef({ typing, approval, ready: state.status === "ready", submit, decide });
  latest.current = { typing, approval, ready: state.status === "ready", submit, decide };
  const onKey = useCallback(
    (input: string, key: Key) => {
      const now = latest.current;
      if (now.approval && now.ready) {
        // y / n answer the approval on screen; nothing else does.
        if (input === "y") void now.decide(now.approval.id, true);
        if (input === "n") void now.decide(now.approval.id, false);
        return;
      }
      if (!now.typing) return;
      // State updaters may run twice in development; side effects stay out of them.
      let text = textRef.current;
      if (key.return) {
        setText("");
        now.submit(text);
        return;
      }
      if (key.backspace || key.delete) return setText(text.slice(0, -1));
      if (key.ctrl || key.meta || key.escape || key.tab) return;
      // One chunk can hold several keys (fast typing, ssh, terminals without bracketed paste):
      // handle each. After a submit Enzo is busy, so the rest of the chunk is dropped.
      for (const ch of input) {
        if (ch === "\r" || ch === "\n") {
          setText("");
          now.submit(text);
          return;
        }
        if (ch >= " ") text += ch;
      }
      setText(text);
    },
    [setText],
  );
  const onPaste = useCallback(
    (pasted: string) => {
      if (latest.current.typing) setText(textRef.current + pasted.replace(/\r?\n/g, " "));
    },
    [setText],
  );
  useInput(onKey);
  usePaste(onPaste);

  if (state.status === "connecting") return <Text dimColor>Connecting to Enzo…</Text>;
  if (state.status === "offline")
    return <Text color="red">Gateway unreachable: {state.error}</Text>;

  return (
    <Box flexDirection="column">
      <Text>
        <Text bold>Enzo</Text>
        <Text dimColor>
          {" "}
          · brain: {state.brain ?? "auto"} · {HELP}
        </Text>
      </Text>
      {state.items.map((item, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
        <Text key={i} dimColor={item.role === "notice"}>
          {item.role === "user" ? (
            <Text color="cyan">you: </Text>
          ) : item.role === "assistant" ? (
            <Text color="green">enzo: </Text>
          ) : null}
          {item.text}
        </Text>
      ))}
      {state.draft && (
        <Text>
          <Text color="green">enzo: </Text>
          {state.draft}
        </Text>
      )}
      {state.working > 0 && <Text dimColor>Working…</Text>}
      {approval && (
        <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
          <Text bold>Approve this action? (y / n)</Text>
          <Text>
            {approval.tool} with exactly: {JSON.stringify(approval.arguments, null, 2)}
          </Text>
        </Box>
      )}
      {state.error && <Text color="red">{state.error}</Text>}
      {info && <Text color="yellow">{info}</Text>}
      <LineInput
        value={text}
        placeholder={state.status === "streaming" ? "Enzo is working…" : "Message Enzo"}
      />
      {state.lastCost !== undefined && <Text dimColor>{formatCost(state.lastCost)}</Text>}
    </Box>
  );
}
