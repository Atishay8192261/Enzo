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
import {
  type Attachment,
  attachedIn,
  chipText,
  type ImageAttacher,
  imagePaths,
  nextTag,
} from "./images.ts";
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
  /** The message as written (pastes put back), and the ids of its images in order. False keeps
   * it in the input. */
  onSubmit: (text: string, images: string[]) => boolean;
  /** Keeps a dragged file's or the clipboard's image (#53); without it, images are not taken. */
  images?: ImageAttacher;
  /** The chat's model can see images (else the chips say so before sending). */
  canSee?: boolean;
  /** Tells the user something about an image (it could not be attached, the clipboard has none). */
  onNotice?: (text: string, error?: boolean) => void;
}

/** Said under the chips when the chat's model cannot see images (issue #53: before sending). */
export const CANT_SEE = "The current model can't see images: choose one that can with /model.";

export function Prompt({
  focused,
  placeholder,
  history,
  sources,
  meta,
  onSubmit,
  images,
  canSee = true,
  onNotice,
}: PromptProps) {
  const renderer = useRenderer();
  const kitty = useKittyKeyboard(renderer);
  const area = useRef<TextareaRenderable | null>(null);
  const pastes = useRef<Paste[]>([]);
  const attachments = useRef<Attachment[]>([]);
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
    const chosen = attachedIn(raw, attachments.current).map((a) => a.image.id);
    if (!onSubmit(message, chosen)) return;
    pastes.current = [];
    attachments.current = [];
    recall.current = null;
    replace("");
  };

  /** An image becomes a tag at the cursor once the gateway has kept it. */
  const attach = async (keep: () => Promise<Attachment["image"] | null>, what: string) => {
    try {
      const image = await keep();
      if (!image) return onNotice?.("The clipboard has no image to paste.");
      const tag = nextTag(attachments.current);
      attachments.current.push({ tag, image });
      const input = area.current;
      if (!input) return;
      input.insertText(`${tag} `);
      setText(input.plainText);
    } catch (e: unknown) {
      const why = e instanceof Error ? e.message.replace(/^.*?HTTP \d+: /, "") : String(e);
      onNotice?.(`Couldn't attach ${what}: ${why}`, true);
    }
  };

  const choose = (s: Suggestion) => {
    if (s.complete) return send(s.value);
    replace(s.value);
    setSelected(0);
  };

  useKeyboard((key) => {
    if (!focused || key.defaultPrevented) return;
    const plain = !key.ctrl && !key.meta && !key.shift;
    // Keys can arrive faster than the screen redraws (fast typing, a paste, a macro): go by what
    // the input holds now, not by what was last drawn, or Enter runs a stale choice (#56).
    const typed = area.current?.plainText ?? text;
    const drawn = typed === text;
    const options = drawn ? suggestions : suggest(typed, sources);
    const open = focused && options.length > 0 && dismissed !== typed;
    const picked = drawn ? choice : options[0];
    if (key.ctrl && key.name === "v" && images) {
      // Ctrl+V pastes an image from the clipboard (Cmd+V pastes text in most terminals).
      key.preventDefault();
      void attach(() => images.clipboard(), "the clipboard's image");
      return;
    }
    if (key.ctrl && key.name === "c" && typed) {
      // Ctrl+C clears what was typed; on an empty input it quits (the app handles that).
      key.preventDefault();
      pastes.current = [];
      attachments.current = [];
      recall.current = null;
      return replace("");
    }
    if (open && picked) {
      if (key.name === "up" || key.name === "down") {
        key.preventDefault();
        const step = key.name === "up" ? -1 : 1;
        return setSelected((i) => (i + step + options.length) % options.length);
      }
      if (key.name === "tab") {
        key.preventDefault();
        replace(picked.value);
        return setSelected(0);
      }
      if ((key.name === "return" || key.name === "kpenter") && plain) {
        key.preventDefault();
        // A whole command name typed out runs as typed: "/model" opens the model picker.
        // With a space after it, an argument is being chosen from the list instead.
        if (COMMAND_LIST.some((c) => c.name === typed)) return send(typed);
        return choose(picked);
      }
      if (key.name === "escape") {
        key.preventDefault();
        return setDismissed(typed);
      }
    }
    if ((key.name === "return" || key.name === "kpenter") && plain && typed.endsWith("\\")) {
      // A backslash before Enter makes a new line, in any terminal (#58). Only one at the very
      // end counts, as in Claude Code: "C:\\" mid-text is just text.
      key.preventDefault();
      const input = area.current;
      if (!input) return;
      input.gotoBufferEnd();
      input.deleteCharBackward();
      input.newLine();
      return setText(input.plainText);
    }
    if (!plain || (key.name !== "up" && key.name !== "down") || open) return;
    // Up and Down bring back earlier messages, when the input is empty or shows one of them.
    const back = recall.current;
    if (back === null && (typed !== "" || key.name === "down")) return;
    if (back !== null && typed !== history[back.at]) return; // edited: arrows move the cursor
    key.preventDefault();
    if (key.name === "up") {
      const at = back === null ? history.length - 1 : back.at - 1;
      if (at < 0) return;
      recall.current = { at, draft: back?.draft ?? typed };
      return replace(history[at] ?? "");
    }
    if (back === null) return;
    const at = back.at + 1;
    if (at >= history.length) {
      recall.current = null;
      return replace(back.draft);
    }
    recall.current = { at, draft: back.draft };
    replace(history[at] ?? "");
  });

  usePaste((event) => {
    if (!focused || !area.current) return;
    // Pasted line breaks never send the message, and a long paste shows as one short tag.
    event.preventDefault();
    const pasted = normalize(stripAnsiSequences(decodePasteBytes(event.bytes)));
    // A file dragged onto the terminal arrives as its path: an image file is attached (#53).
    const paths = images ? imagePaths(pasted) : null;
    if (images && paths) {
      for (const path of paths) void attach(() => images.file(path), path.split("/").pop() ?? path);
      return;
    }
    if (images && pasted.trim() === "") {
      // Some terminals paste nothing when the clipboard holds only an image: read it instead.
      void attach(() => images.clipboard(), "the clipboard's image");
      return;
    }
    if (shouldCollapse(pasted)) {
      const tag = tagFor(pasted, pastes.current);
      pastes.current.push({ tag, text: pasted });
      area.current.insertText(tag);
    } else {
      area.current.insertText(pasted);
    }
    setText(area.current.plainText);
  });

  const chips = attachedIn(text, attachments.current);
  return (
    <box flexDirection="column" flexShrink={0}>
      {popup ? (
        <Popup suggestions={suggestions} selected={Math.min(selected, suggestions.length - 1)} />
      ) : null}
      {chips.length > 0 ? (
        <box flexDirection="column" paddingLeft={2} flexShrink={0}>
          <text fg={COLOR.accent} wrapMode="none">
            {clipped(`▣ ${chips.map(chipText).join("  ")}`, Math.max(10, width - 4))}
          </text>
          {canSee ? null : <text fg={COLOR.error}>{CANT_SEE}</text>}
        </box>
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
