import {
  type ChatItem,
  describeStats,
  formatClock,
  formatDuration,
  STEP_MARK,
  stepLine,
} from "@enzo/chat";
import type { SyntaxStyle } from "@opentui/core";
import { useEffect, useState } from "react";
import type { Reply } from "./commands.ts";
import { COLOR, fg } from "./theme.ts";

/** A command's output, kept in the chat under the command that made it (#36). */
export interface Card {
  id: number;
  /** Shown after this many chat items. */
  at: number;
  /** The command as typed, e.g. "/models". */
  command: string;
  reply: Exclude<Reply, null>;
}

const FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";

export function Spinner({ color = COLOR.accent }: { color?: string }) {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setFrame((f) => (f + 1) % FRAMES.length), 80);
    return () => clearInterval(timer);
  }, []);
  return <span fg={color}>{FRAMES[frame]}</span>;
}

/** Enzo's side of a turn starts with its name; what follows hangs under it. */
export function EnzoHeader() {
  return (
    <box marginTop={1} flexShrink={0}>
      <text>
        <span fg={COLOR.accent}>◆ </span>
        <strong>enzo</strong>
      </text>
    </box>
  );
}

export function Item({
  item,
  markdown,
  details,
}: {
  item: ChatItem;
  markdown: SyntaxStyle;
  /** Show the facts behind each message: times, tokens, cost (#49). */
  details: boolean;
}) {
  switch (item.role) {
    case "user":
      return <UserMessage item={item} details={details} />;
    case "assistant":
      return <ReplyBlock item={item} markdown={markdown} details={details} />;
    case "step":
      return <Step item={item} details={details} />;
    case "notice":
      return <Notice item={item} details={details} />;
  }
}

/** The user's message as a card, with when it was sent. */
function UserMessage({ item, details }: { item: ChatItem; details: boolean }) {
  const when = details && item.at ? ` · ${formatClock(item.at)}` : "";
  return (
    <box
      border
      borderStyle="rounded"
      borderColor={item.unsent ? COLOR.error : COLOR.faint}
      title={item.unsent ? " you · not sent " : ` you${when} `}
      titleAlignment="left"
      paddingLeft={1}
      paddingRight={1}
      marginTop={1}
      flexShrink={0}
    >
      <text>{item.text}</text>
    </box>
  );
}

/** Enzo's words as formatted Markdown, then the facts behind them on one dim line (#36, #49). */
export function ReplyBlock({
  item,
  markdown,
  details,
  streaming = false,
}: {
  item: ChatItem;
  markdown: SyntaxStyle;
  details: boolean;
  streaming?: boolean;
}) {
  // Who answered is always shown, on its own line (#36); the rest only with details on.
  const model = item.stats?.usage.models.at(-1) ?? item.by;
  const facts = details ? describeStats(item) : [];
  const shown = facts.length > 0 ? facts : model ? [model] : [];
  return (
    <box paddingLeft={2} flexShrink={0} flexDirection="column">
      <markdown content={item.text} syntaxStyle={markdown} conceal streaming={streaming} />
      {shown.length > 0 ? <Facts parts={shown} /> : null}
    </box>
  );
}

/** Facts in one dim line, each part kept whole: a narrow terminal wraps between parts. */
function Facts({ parts }: { parts: string[] }) {
  return (
    <box flexDirection="row" flexWrap="wrap" flexShrink={0}>
      {parts.map((part, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: parts of a fixed line
        <text key={i} fg={COLOR.faint} wrapMode="none">
          {i === 0 ? `└ ${part}` : ` · ${part}`}
        </text>
      ))}
    </box>
  );
}

const STEP_COLOR = {
  running: COLOR.accent,
  ok: COLOR.ok,
  failed: COLOR.error,
  skipped: COLOR.dim,
  stopped: COLOR.warn,
} as const;

/**
 * One thing Enzo did, in plain words (#40), on a thin timeline: a spinner while it runs,
 * then how it went and how long it took (#49).
 */
function Step({ item, details }: { item: ChatItem; details: boolean }) {
  const status = item.status ?? "ok";
  const took =
    details && item.ms !== undefined && status !== "running" ? `  ${formatDuration(item.ms)}` : "";
  return (
    <box paddingLeft={2} flexShrink={0}>
      <text {...fg(status === "running" ? undefined : COLOR.dim)}>
        <span fg={COLOR.faint}>│ </span>
        {status === "running" ? (
          <Spinner />
        ) : (
          <span fg={STEP_COLOR[status]}>{STEP_MARK[status]}</span>
        )}{" "}
        {stepLine(item)}
        <span fg={COLOR.faint}>{took}</span>
      </text>
    </box>
  );
}

function Notice({ item, details }: { item: ChatItem; details: boolean }) {
  const facts = details ? describeStats(item) : [];
  return (
    <box paddingLeft={2} marginTop={1} flexShrink={0} flexDirection="column">
      <text fg={COLOR.dim}>
        {item.text}
        {item.offer === "stop" ? <span fg={COLOR.warn}> Esc stops it.</span> : null}
        {item.offer === "resend" ? (
          <span fg={COLOR.warn}> /retry sends your message again.</span>
        ) : null}
      </text>
      {facts.length > 0 ? <Facts parts={facts} /> : null}
    </box>
  );
}

/** Enzo is thinking: nothing streamed yet and no step running. */
export function Thinking() {
  return (
    <box paddingLeft={2} flexShrink={0}>
      <text fg={COLOR.dim}>
        <span fg={COLOR.faint}>│ </span>
        <Spinner /> Thinking…
      </text>
    </box>
  );
}

/** A command and what it showed, as one card: never loose text that reads like a reply (#36). */
export function CommandCard({ card }: { card: Card }) {
  const { reply } = card;
  const error = typeof reply === "object" && "error" in reply;
  return (
    <box
      border
      borderStyle="rounded"
      borderColor={error ? COLOR.error : COLOR.accentDim}
      title={` ${card.command} `}
      titleAlignment="left"
      paddingLeft={1}
      paddingRight={1}
      marginTop={1}
      flexDirection="column"
      flexShrink={0}
    >
      {typeof reply === "string" ? <Lines text={reply} /> : null}
      {typeof reply === "object" && "text" in reply ? (
        <Lines text={reply.text} color={COLOR.error} />
      ) : null}
      {typeof reply === "object" && "rows" in reply ? <Rows rows={reply.rows} /> : null}
    </box>
  );
}

/**
 * One text per line: a long line wraps within itself, and a line that is only a link (a
 * sign-in or usage address) is never joined to other words (#36).
 */
function Lines({ text, color }: { text: string; color?: string }) {
  return (
    <>
      {text.split("\n").map((line, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: lines of a fixed text
        <text key={i} {...fg(color ?? (isLink(line) ? COLOR.link : undefined))}>
          {line || " "}
        </text>
      ))}
    </>
  );
}

const isLink = (line: string) => /^https?:\/\/\S+$/.test(line.trim());

/** Two columns: a wrapped description stays under its own column (#36). */
function Rows({ rows }: { rows: [string, string][] }) {
  const width = Math.min(32, Math.max(...rows.map(([left]) => left.length)) + 2);
  return (
    <>
      {rows.map(([left, right], i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows of a fixed table
        <box key={i} flexDirection="row" flexShrink={0}>
          <box width={width} flexShrink={0}>
            <text fg={COLOR.accent}>{left || " "}</text>
          </box>
          <box flexGrow={1} flexShrink={1}>
            <text fg={COLOR.dim}>{right || " "}</text>
          </box>
        </box>
      ))}
    </>
  );
}
