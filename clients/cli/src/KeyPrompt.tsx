import { decodePasteBytes } from "@opentui/core";
import { useKeyboard, usePaste } from "@opentui/react";
import { useRef, useState } from "react";

/**
 * Reads a secret without ever drawing it: typed or pasted characters show as dots only.
 * The key lives in a ref, not React state, and is handed over once on Enter.
 */
export function KeyPrompt({
  provider,
  onSubmit,
  onCancel,
}: {
  provider: string;
  onSubmit: (key: string) => void;
  onCancel: () => void;
}) {
  const key = useRef("");
  const [length, setLength] = useState(0);
  const update = (next: string) => {
    key.current = next;
    setLength(next.length);
  };

  useKeyboard((e) => {
    if (e.name === "escape") return onCancel();
    if (e.name === "return" || e.name === "enter" || e.name === "linefeed") {
      const value = key.current.trim();
      update("");
      if (value) onSubmit(value);
      return;
    }
    if (e.name === "backspace") return update(key.current.slice(0, -1));
    if (e.ctrl && e.name === "u") return update("");
    if (!e.ctrl && !e.meta && e.sequence.length === 1 && e.sequence >= " ") {
      update(key.current + e.sequence);
    }
  });
  usePaste((e) => update(key.current + decodePasteBytes(e.bytes).replace(/\s+/g, "")));

  return (
    <box
      border
      borderColor="cyan"
      title={`Paste your ${provider} API key (hidden) · Enter saves · Esc cancels`}
      height={3}
    >
      <text>{length ? `${"•".repeat(Math.min(length, 40))} ${length} characters` : " "}</text>
    </box>
  );
}
