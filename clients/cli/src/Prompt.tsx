import {
  decodePasteBytes,
  type KeyBinding,
  stripAnsiSequences,
  type TextareaRenderable,
} from "@opentui/core";
import { useKeyboard, usePaste, useRenderer, useTerminalDimensions } from "@opentui/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { COMMAND_LIST } from "./commands.ts";
import { type Sources, type Suggestion, suggest } from "./complete.ts";
import { expand, normalize, type Paste, shouldCollapse, tagFor } from "./paste.ts";
import { bg, COLOR } from "./theme.ts";

/**
 * Enter sends. A new line: Shift+Enter or Option/Alt+Enter where the terminal reports them,
 * and Ctrl+J everywhere (it is a plain line feed in every terminal) (#35).
 */
export const PROMPT_KEYS: KeyBinding[] = [
  { name: "return", action: "submit" },
  { name: "kpenter", action: "submit" },
  { name: "return", shift: true, action: "newline" },
  { name: "return", meta: true, action: "newline" },
  { name: "linefeed", action: "newline" },
  { name: "j", ctrl: true, action: "newline" },
];

/**
 * What the line under the input says about new lines. Shift+Enter only where the terminal tells
 * it apart from Enter (the kitty keyboard protocol); a backslash before Enter works everywhere,
 * including macOS Terminal.app (#58).
 */
export const HINT_KITTY = "enter send · shift+enter new line";
export const HINT_PLAIN = "enter send · \\ then enter new line";

const clipped = (s: string, n: number) =>
  s.length <= n ? s : `${s.slice(0, Math.max(0, n - 1))}…`;

/** The popup shows this many lines at most; it scrolls with the selection. */
const POPUP_ROWS = 8;

export interface PromptProps {
  focused: boolean;
  placeholder: string;
  /** Messages sent in this chat, oldest first: Up and Down bring them back. */
  history: readonly string[];
  sources: Sources;
  /** The line under the input: model and account. */
  meta: string;
  /** The message as written (pastes put back). False keeps it in the input. */
  onSubmit: (text: string) => boolean;
}

export function Prompt({ focused, placeholder, history, sources, meta, onSubmit }: PromptProps) {
  const renderer = useRenderer();
  const kitty = useKittyKeyboard(renderer);
  const area = useRef<TextareaRenderable | null>(null);
  const pastes = useRef<Paste[]>([]);
  const [text, setText] = useState("");
  // Up/Down through earlier messages: where we are, and what was typed before.
  const recall = useRef<{ at: number; draft: string } | null>(null);
  const [selected, setSelected] = useState(0);
  const [dismissed, setDismissed] = useState<string | null>(null);

  const suggestions = useMemo(() => suggest(text, sources), [text, sources]);
  // The line under the input: the hint gives way before the model and account are cut.
  const { width } = useTerminalDimensions();
  const room = Math.max(0, Math.min(width, 84) - 10);
  const HINT = kitty ? HINT_KITTY : HINT_PLAIN;
  const hint = meta.length + HINT.length + 2 <= room ? HINT : "";
  const popup = focused && suggestions.length > 0 && dismissed !== text;
  const choice = suggestions[Math.min(selected, suggestions.length - 1)];

  const replace = (value: string) => {
    const input = area.current;
    if (!input) return;
    input.setText(value);
    input.gotoBufferEnd();
    setText(value);
  };

  const send = (raw: string) => {
    const message = expand(raw, pastes.current);
    if (!onSubmit(message)) return;
    pastes.current = [];
    recall.current = null;
    replace("");
  };

  // Only a backslash at the very end counts, as in Claude Code: "C:\\" mid-text is just text.
  const endsWithBackslash = () => (area.current?.plainText ?? text).endsWith("\\");

  const choose = (s: Suggestion) => {
    if (s.complete) return send(s.value);
    replace(s.value);
    setSelected(0);
  };

  useKeyboard((key) => {
    if (!focused || key.defaultPrevented) return;
    const plain = !key.ctrl && !key.meta && !key.shift;
    if (key.ctrl && key.name === "c" && text) {
      // Ctrl+C clears what was typed; on an empty input it quits (the app handles that).
      key.preventDefault();
      pastes.current = [];
      recall.current = null;
      return replace("");
    }
    if (popup && choice) {
      if (key.name === "up" || key.name === "down") {
        key.preventDefault();
        const step = key.name === "up" ? -1 : 1;
        return setSelected((i) => (i + step + suggestions.length) % suggestions.length);
      }
      if (key.name === "tab") {
        key.preventDefault();
        replace(choice.value);
        return setSelected(0);
      }
      if ((key.name === "return" || key.name === "kpenter") && plain) {
        key.preventDefault();
        // A whole command name typed out runs as typed: "/model" opens the model picker.
        // With a space after it, an argument is being chosen from the list instead.
        if (COMMAND_LIST.some((c) => c.name === text)) return send(text);
        return choose(choice);
      }
      if (key.name === "escape") {
        key.preventDefault();
        return setDismissed(text);
      }
    }
    if ((key.name === "return" || key.name === "kpenter") && plain && endsWithBackslash()) {
      // A backslash before Enter makes a new line, in any terminal (#58).
      key.preventDefault();
      const input = area.current;
      if (!input) return;
      input.gotoBufferEnd();
      input.deleteCharBackward();
      input.newLine();
      return setText(input.plainText);
    }
    if (!plain || (key.name !== "up" && key.name !== "down") || popup) return;
    // Up and Down bring back earlier messages, when the input is empty or shows one of them.
    const now = recall.current;
    if (now === null && (text !== "" || key.name === "down")) return;
    if (now !== null && text !== history[now.at]) return; // edited: arrows move the cursor
    key.preventDefault();
    if (key.name === "up") {
      const at = now === null ? history.length - 1 : now.at - 1;
      if (at < 0) return;
      recall.current = { at, draft: now?.draft ?? text };
      return replace(history[at] ?? "");
    }
    if (now === null) return;
    const at = now.at + 1;
    if (at >= history.length) {
      recall.current = null;
      return replace(now.draft);
    }
    recall.current = { at, draft: now.draft };
    replace(history[at] ?? "");
  });

  usePaste((event) => {
    if (!focused || !area.current) return;
    // Pasted line breaks never send the message, and a long paste shows as one short tag.
    event.preventDefault();
    const pasted = normalize(stripAnsiSequences(decodePasteBytes(event.bytes)));
    if (shouldCollapse(pasted)) {
      const tag = tagFor(pasted, pastes.current);
      pastes.current.push({ tag, text: pasted });
      area.current.insertText(tag);
    } else {
      area.current.insertText(pasted);
    }
    setText(area.current.plainText);
  });

  return (
    <box flexDirection="column" flexShrink={0}>
      {popup ? (
        <Popup suggestions={suggestions} selected={Math.min(selected, suggestions.length - 1)} />
      ) : null}
      <box
        border
        borderStyle="rounded"
        borderColor={focused ? COLOR.accent : COLOR.faint}
        paddingLeft={1}
        paddingRight={1}
        flexDirection="row"
        flexShrink={0}
      >
        <text fg={focused ? COLOR.accent : COLOR.faint}>❯ </text>
        <textarea
          ref={area}
          focused={focused}
          flexGrow={1}
          placeholder={placeholder}
          placeholderColor={COLOR.faint}
          keyBindings={PROMPT_KEYS}
          minHeight={1}
          maxHeight={8}
          onContentChange={() => {
            setText(area.current?.plainText ?? "");
            setSelected(0);
          }}
          onSubmit={() => send(area.current?.plainText ?? "")}
        />
      </box>
      <box flexDirection="row" justifyContent="space-between" paddingLeft={2} paddingRight={1}>
        <text fg={COLOR.dim} wrapMode="none">
          {clipped(meta, room - (hint ? hint.length + 2 : 0))}
        </text>
        {hint ? (
          <text fg={COLOR.faint} wrapMode="none">
            {hint}
          </text>
        ) : null}
      </box>
    </box>
  );
}

function Popup({ suggestions, selected }: { suggestions: Suggestion[]; selected: number }) {
  const start = Math.max(0, Math.min(selected - POPUP_ROWS + 1, suggestions.length - POPUP_ROWS));
  const shown = suggestions.slice(start, start + POPUP_ROWS);
  const width = Math.min(30, Math.max(...shown.map((s) => s.label.length)) + 2);
  return (
    <box
      border
      borderStyle="rounded"
      borderColor={COLOR.faint}
      flexDirection="column"
      flexShrink={0}
      {...(suggestions.length > POPUP_ROWS
        ? { title: ` ${selected + 1}/${suggestions.length} ` }
        : {})}
      titleAlignment="right"
    >
      {shown.map((s, i) => {
        const on = start + i === selected;
        return (
          <box
            key={s.value}
            flexDirection="row"
            {...bg(on ? COLOR.selectedBg : undefined)}
            paddingLeft={1}
            flexShrink={0}
          >
            <box width={width} flexShrink={0} overflow="hidden" paddingRight={1}>
              <text fg={on ? COLOR.selectedFg : COLOR.accent} wrapMode="none">
                {s.label}
              </text>
            </box>
            <box flexGrow={1} flexShrink={1}>
              <text fg={on ? COLOR.selectedFg : COLOR.dim} wrapMode="none">
                {s.description}
              </text>
            </box>
          </box>
        );
      })}
      <box paddingLeft={1}>
        <text fg={COLOR.faint}>↑↓ choose · tab complete · enter run · esc close</text>
      </box>
    </box>
  );
}

/** Whether the terminal reports keys with the kitty protocol (known once it has answered). */
function useKittyKeyboard(renderer: ReturnType<typeof useRenderer>): boolean {
  const [kitty, setKitty] = useState(renderer.capabilities?.kitty_keyboard === true);
  useEffect(() => {
    const update = () => setKitty(renderer.capabilities?.kitty_keyboard === true);
    renderer.on("capabilities", update);
    return () => {
      renderer.off("capabilities", update);
    };
  }, [renderer]);
  return kitty;
}
