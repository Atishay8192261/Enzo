/**
 * Long pastes show as a short tag in the input, like Claude Code and Codex: "[Pasted 42 lines]".
 * The tag stands for the text; sending puts the text back in its place (#35).
 */

/** A paste this long (lines or characters) is shown as a tag. */
export const COLLAPSE_LINES = 3;
export const COLLAPSE_CHARS = 800;

export interface Paste {
  tag: string;
  text: string;
}

/** Terminals send pasted line breaks as \r or \r\n. */
export function normalize(text: string): string {
  return text.replace(/\r\n?/g, "\n");
}

export function shouldCollapse(text: string): boolean {
  return text.split("\n").length >= COLLAPSE_LINES || text.length >= COLLAPSE_CHARS;
}

/** The tag for a paste; the second and later pastes are numbered so each stays distinct. */
export function tagFor(text: string, earlier: readonly Paste[]): string {
  const lines = text.split("\n").length;
  const what = lines > 1 ? `${lines} lines` : `${text.length} characters`;
  const n = earlier.length + 1;
  return n === 1 ? `[Pasted ${what}]` : `[Pasted ${what} #${n}]`;
}

/** The message as written: each tag still in it becomes its pasted text again. */
export function expand(input: string, pastes: readonly Paste[]): string {
  if (pastes.length === 0) return input;
  const byTag = new Map(pastes.map((p) => [p.tag, p.text]));
  // One pass: a pasted text that happens to contain a tag is never expanded again.
  const any = new RegExp(
    pastes.map((p) => p.tag.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"),
    "g",
  );
  return input.replace(any, (tag) => byTag.get(tag) ?? tag);
}
