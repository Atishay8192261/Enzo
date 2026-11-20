import {
  type ChatState,
  describeChoice,
  describeTotals,
  formatDuration,
  formatSpend,
  formatTokens,
} from "@enzo/chat";
import type { ChatSummary, ProviderConnection } from "@enzo/sdk";
import type { ReactNode } from "react";
import { accountName } from "./complete.ts";
import { Logo } from "./Logo.tsx";
import { COLOR, fg } from "./theme.ts";

/** The model choice, short: "auto", "any fast model", "claude-sonnet". */
export function modelWords(state: ChatState): string {
  if (state.brain === null) return "auto";
  return describeChoice(state.brain, state.brains).replace(/ \(.*\)$/, "");
}

/** Who pays, short: "ChatGPT plan", "anthropic · API key", or how to connect one. */
export function accountWords(connections: ProviderConnection[]): string {
  const models = connections.filter((c) => c.connected && c.models.length > 0);
  if (models.length === 0) return "no account yet: /login";
  const plan = models.find((c) => c.method === "chatgpt");
  const first = plan ?? models[0];
  const more = models.length > 1 ? ` +${models.length - 1}` : "";
  return first ? `${accountName(first)}${more}` : "";
}

/** The chat's title: its name, or else its first message, cut to fit. */
export function chatTitle(state: ChatState, width: number): string {
  const first = state.items
    .find((i) => i.role === "user")
    ?.text.split("\n")[0]
    ?.trim();
  const name = state.title ?? first;
  return name ? clip(name, width) : "New chat";
}

/** When something happened, roughly: "just now", "5 min ago", "3 h ago", "2 days ago". */
export function ago(iso: string, now = Date.now()): string {
  const minutes = Math.max(0, Math.round((now - Date.parse(iso)) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export const isOnline = (state: ChatState) => state.online && state.status !== "offline";

/** The top line: Enzo, this chat, and whether Enzo can be reached (#35, #44). */
export function Header({ state, width }: { state: ChatState; width: number }) {
  const online = isOnline(state);
  const status =
    state.status === "connecting" ? "connecting" : online ? "connected" : "offline · retrying";
  return (
    <box
      flexDirection="row"
      justifyContent="space-between"
      paddingLeft={2}
      paddingRight={2}
      flexShrink={0}
    >
      <text wrapMode="none">
        <span fg={COLOR.accent}>◆ </span>
        <strong>ENZO</strong>
        <span fg={COLOR.faint}>{"  │  "}</span>
        <span fg={COLOR.dim}>{chatTitle(state, Math.max(10, width - 40))}</span>
      </text>
      <text wrapMode="none">
        <span fg={online ? COLOR.ok : COLOR.error}>{online ? "● " : "○ "}</span>
        <span fg={online ? COLOR.dim : COLOR.error}>{status}</span>
      </text>
    </box>
  );
}

/**
 * The bottom line, always on screen: which model, which account, what this chat used, and the
 * keys that matter (#35, #49). A short message (copied, …) takes the right side for a moment.
 */
export function StatusBar({
  state,
  connections,
  width,
  flash,
}: {
  state: ChatState;
  connections: ProviderConnection[];
  width: number;
  flash: string | null;
}) {
  const used = describeTotals(state.totals);
  const parts: [string, string][] = [
    [modelWords(state), COLOR.accent],
    [` · ${accountWords(connections)}`, COLOR.dim],
    ...(used ? ([[` · this chat: ${used}`, COLOR.dim]] as [string, string][]) : []),
  ];
  // The two sides never run into each other: the hints go first when space runs out, then the
  // left side is cut with an ellipsis. A short message (copied, …) always shows.
  const room = Math.max(0, width - 4);
  const full = parts.map(([t]) => t).join("").length;
  const keys = [
    "/ commands · ctrl+p palette · esc stop · ctrl+d quit",
    "ctrl+p · ctrl+d quit",
  ].find((k) => full + k.length + 2 <= room);
  const right = flash ?? keys ?? "";
  const left = fit(parts, room - (right ? right.length + 2 : 0));
  return (
    <box
      flexDirection="row"
      justifyContent="space-between"
      paddingLeft={2}
      paddingRight={2}
      flexShrink={0}
    >
      <text wrapMode="none">
        {left.map(([text, color], i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed parts of one line
          <span key={i} fg={color}>
            {text}
          </span>
        ))}
      </text>
      {right ? (
        <text fg={flash ? COLOR.ok : COLOR.faint} wrapMode="none">
          {right}
        </text>
      ) : null}
    </box>
  );
}

/** Coloured parts cut to `room` characters, the last one ending in an ellipsis. */
export function fit(parts: [string, string][], room: number): [string, string][] {
  const out: [string, string][] = [];
  let left = room;
  for (const [text, color] of parts) {
    if (left <= 0) break;
    if (text.length <= left) {
      out.push([text, color]);
      left -= text.length;
    } else {
      out.push([`${text.slice(0, Math.max(0, left - 1))}…`, color]);
      break;
    }
  }
  return out;
}

/** Below this width the side panel is hidden unless asked for. */
export const SIDEBAR_AUTO_WIDTH = 120;
export const SIDEBAR_WIDTH = 36;

/** The side panel: what this chat used, the model, the accounts, the keys (#35, #49). */
export function Sidebar({
  state,
  connections,
}: {
  state: ChatState;
  connections: ProviderConnection[];
}) {
  const t = state.totals;
  const replies = state.items.filter((i) => i.stats && i.stats.usage.calls > 0).length;
  return (
    <box
      width={SIDEBAR_WIDTH}
      flexShrink={0}
      flexDirection="column"
      paddingLeft={2}
      paddingRight={1}
      border={["left"]}
      borderColor={COLOR.faint}
    >
      <Section title="This chat used">
        {t.calls === 0 ? <text fg={COLOR.dim}>nothing yet</text> : null}
        {t.calls > 0 ? (
          <>
            <Pair left="tokens in" right={formatTokens(t.input_tokens)} />
            <Pair left="tokens out" right={formatTokens(t.output_tokens)} />
            {t.cached_input_tokens > 0 ? (
              <Pair left="from cache" right={formatTokens(t.cached_input_tokens)} />
            ) : null}
            <Pair left="model calls" right={String(t.calls)} />
            {replies > 0 ? <Pair left="replies" right={String(replies)} /> : null}
            <Pair left="model time" right={formatDuration(t.model_ms)} />
            <Pair left="paid" right={formatSpend(t) || "nothing"} />
            {t.models.length > 0 ? <text fg={COLOR.dim}>{t.models.join(", ")}</text> : null}
          </>
        ) : null}
      </Section>
      <Section title="Model">
        <text fg={COLOR.dim}>{describeChoice(state.brain, state.brains)}</text>
      </Section>
      <Section title="Accounts">
        {connections.length === 0 ? <text fg={COLOR.dim}>…</text> : null}
        {connections.map((c) => (
          <text key={c.provider} {...fg(c.connected ? undefined : COLOR.dim)} wrapMode="none">
            <span fg={c.connected ? COLOR.ok : COLOR.faint}>{c.connected ? "● " : "○ "}</span>
            {c.connected ? accountName(c) : c.provider}
          </text>
        ))}
      </Section>
      <Section title="Keys">
        <text fg={COLOR.dim}>/ commands · ctrl+p all</text>
        <text fg={COLOR.dim}>esc stop · ↑ earlier messages</text>
        <text fg={COLOR.dim}>/details · /sidebar hide this</text>
      </Section>
    </box>
  );
}

function Pair({ left, right }: { left: string; right: string }) {
  return (
    <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
      <text fg={COLOR.dim}>{left}</text>
      <text>{right}</text>
    </box>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <box flexDirection="column" paddingTop={1} flexShrink={0}>
      <text fg={COLOR.accent}>{title}</text>
      {children}
    </box>
  );
}

const clip = (s: string, n: number) => (s.length <= n ? s : `${s.slice(0, n - 1)}…`);

/**
 * The opening screen for a new or empty chat (#35): the ENZO wordmark, the version, the
 * account and model, a few keys, and the input right under it. It gives way to the chat as
 * soon as the first message is sent.
 */
export function Home({
  state,
  connections,
  lastChat,
  version,
  width,
  height,
  prompt,
}: {
  state: ChatState;
  connections: ProviderConnection[];
  /** The most recent past chat, offered to continue (#57). */
  lastChat: ChatSummary | null;
  version: string;
  width: number;
  height: number;
  prompt: ReactNode;
}) {
  const connected = connections.some((c) => c.connected && c.models.length > 0);
  return (
    <box flexDirection="column" flexGrow={1} alignItems="center" paddingLeft={2} paddingRight={2}>
      <box flexGrow={1} minHeight={0} />
      <Logo width={width} height={height} />
      <text fg={COLOR.dim}>your personal agent · {version}</text>
      <box height={1} />
      <box width="100%" maxWidth={80} flexDirection="column" flexShrink={0}>
        {prompt}
      </box>
      <box height={1} />
      {state.status === "connecting" ? <text fg={COLOR.dim}>Connecting to Enzo…</text> : null}
      {state.status !== "connecting" && !connected ? (
        // Short lines: a command a user must type is never wrapped in half (#36).
        <box flexDirection="column" alignItems="center" flexShrink={0}>
          <text fg={COLOR.warn}>No model account is connected yet. Connect one:</text>
          <text fg={COLOR.warn}>your ChatGPT plan, no key needed: /login chatgpt</text>
          <text fg={COLOR.warn}>an API key: /login anthropic, openai, xai or moonshot</text>
        </box>
      ) : null}
      {lastChat ? (
        <text fg={COLOR.dim} wrapMode="none">
          Continue “{clip(lastChat.title ?? "Untitled chat", 40)}” ({ago(lastChat.last_active)}):{" "}
          <span fg={COLOR.accent}>ctrl+r</span> or /chats
        </text>
      ) : null}
      <text fg={COLOR.faint}>
        / commands · ctrl+p palette · /new new chat · /help · ctrl+d quit
      </text>
      <box flexGrow={1} minHeight={0} />
    </box>
  );
}
