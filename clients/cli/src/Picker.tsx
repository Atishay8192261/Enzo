import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/react";
import { useState } from "react";
import { bg, COLOR, fg, panel } from "./theme.ts";

export interface Choice {
  label: string;
  description?: string;
  /** The command line it runs, e.g. "/model fast". */
  value: string;
  /** A heading above the first choice of each group. */
  group?: string;
  /** Shown dim: it works only after something else (an account) is connected. */
  muted?: boolean;
}

const ROWS = 12;

/**
 * A list over the chat, like OpenCode's dialogs: type to filter, Up/Down and Enter to pick,
 * Esc to close (#35). Used for the command palette, the model picker and account pickers.
 */
export function Picker({
  title,
  choices,
  onPick,
  onClose,
}: {
  title: string;
  choices: Choice[];
  onPick: (choice: Choice) => void;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState(0);
  const { width } = useTerminalDimensions();
  const { themeMode } = useRenderer();
  const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = choices.filter((c) => {
    const hay = `${c.label} ${c.description ?? ""} ${c.group ?? ""}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
  const at = Math.min(selected, Math.max(0, shown.length - 1));
  // A list of plain choices (yes / no) gives its labels the whole width.
  const wide = choices.every((c) => !c.description);

  useKeyboard((key) => {
    if (key.defaultPrevented) return;
    if (key.name === "escape") {
      key.preventDefault();
      return onClose();
    }
    if (key.name === "up" || key.name === "down") {
      key.preventDefault();
      if (shown.length === 0) return;
      const step = key.name === "up" ? -1 : 1;
      return setSelected((at + step + shown.length) % shown.length);
    }
    if (key.name === "return" || key.name === "kpenter") {
      key.preventDefault();
      const picked = shown[at];
      if (picked) onPick(picked);
    }
  });

  const start = Math.max(0, Math.min(at - ROWS + 1, shown.length - ROWS));
  const visible = shown.slice(start, start + ROWS);
  return (
    <box position="absolute" top={2} left={0} width="100%" alignItems="center" zIndex={100}>
      <box
        border
        borderStyle="rounded"
        borderColor={COLOR.accent}
        title={` ${title} `}
        width={Math.min(76, width - 4)}
        flexDirection="column"
        paddingLeft={1}
        paddingRight={1}
        backgroundColor={panel(themeMode)}
      >
        <input
          focused
          placeholder="Type to filter"
          placeholderColor={COLOR.faint}
          onInput={(value: string) => {
            setFilter(value);
            setSelected(0);
          }}
        />
        <box height={1} />
        {visible.length === 0 ? <text fg={COLOR.dim}>Nothing matches.</text> : null}
        {visible.map((c, i) => {
          const on = start + i === at;
          const heading = c.group && c.group !== visible[i - 1]?.group ? c.group : null;
          return (
            <box key={c.value} flexDirection="column" flexShrink={0}>
              {heading ? (
                <text fg={COLOR.dim}>
                  <strong>{heading}</strong>
                </text>
              ) : null}
              <box
                flexDirection="row"
                {...bg(on ? COLOR.selectedBg : undefined)}
                paddingLeft={1}
                flexShrink={0}
              >
                <box
                  {...(wide ? { flexGrow: 1 } : { width: 28 })}
                  flexShrink={0}
                  overflow="hidden"
                  paddingRight={1}
                >
                  <text
                    {...fg(on ? COLOR.selectedFg : c.muted ? COLOR.dim : undefined)}
                    wrapMode="none"
                  >
                    {c.label}
                  </text>
                </box>
                <box flexGrow={1} flexShrink={1}>
                  <text fg={on ? COLOR.selectedFg : COLOR.dim} wrapMode="none">
                    {c.description ?? ""}
                  </text>
                </box>
              </box>
            </box>
          );
        })}
        <box paddingTop={1}>
          <text fg={COLOR.faint}>↑↓ choose · enter pick · esc close</text>
        </box>
      </box>
    </box>
  );
}
