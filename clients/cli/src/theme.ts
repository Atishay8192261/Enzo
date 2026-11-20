import { type NativeResourceOwner, SyntaxStyle } from "@opentui/core";

/**
 * Enzo's colours. No background fills: the terminal's own background shows through, so the
 * app reads the same in light and dark terminals.
 */
export const COLOR = {
  accent: "#8b7cf6",
  accent2: "#4fb6d8",
  accentDim: "#5b5196",
  dim: "#8a8a8a",
  faint: "#5c5c5c",
  ok: "#3fb950",
  error: "#f05a4f",
  warn: "#d6a222",
  link: "#4fb6d8",
  selectedFg: "#ffffff",
  selectedBg: "#6c5ce7",
} as const;

/** A dialog's own background, so the chat under it does not show through. */
export function panel(mode: "dark" | "light" | null): string {
  return mode === "light" ? "#f2f1f7" : "#1c1b24";
}

/** The wordmark's colours, top to bottom. */
export const WORDMARK = [COLOR.accent, COLOR.accent2];

/** How Markdown in replies looks: bold, italic, code, links, lists, quotes (#36). */
export function markdownStyle(owner: NativeResourceOwner): SyntaxStyle {
  return SyntaxStyle.fromStyles(
    {
      default: {},
      conceal: { fg: COLOR.faint },
      "markup.strong": { bold: true },
      "markup.italic": { italic: true },
      "markup.strikethrough": { strikethrough: true },
      "markup.raw": { fg: COLOR.warn },
      "markup.raw.block": {},
      "markup.heading": { fg: COLOR.accent, bold: true },
      "markup.link.label": { fg: COLOR.link, underline: true },
      "markup.link.url": { fg: COLOR.dim },
      "markup.list": { fg: COLOR.accent },
      "markup.quote": { fg: COLOR.dim, italic: true },
      // fenced code
      keyword: { fg: "#c678dd" },
      "keyword.function": { fg: "#c678dd" },
      string: { fg: "#98c379" },
      comment: { fg: COLOR.dim, italic: true },
      number: { fg: "#d19a66" },
      boolean: { fg: "#d19a66" },
      constant: { fg: "#d19a66" },
      function: { fg: "#61afef" },
      "function.call": { fg: "#61afef" },
      "function.method": { fg: "#61afef" },
      type: { fg: "#e5c07b" },
      "type.builtin": { fg: "#e5c07b" },
      property: { fg: "#e06c75" },
      operator: { fg: "#56b6c2" },
    },
    owner,
  );
}

/** Optional colours as props: a missing colour is left out, so the terminal's own shows. */
export const fg = (color: string | undefined) => (color ? { fg: color } : {});
export const bg = (color: string | undefined) => (color ? { backgroundColor: color } : {});
