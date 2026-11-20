import type { MarkdownInlineContext, MarkdownInlineSpan } from "@opentui/core";

/** A URL at least this long goes on its own line; a shorter one rarely meets a line's end. */
export const LONG_URL = 30;

/**
 * Puts each long URL in a reply on a line of its own (#56). In the middle of a sentence, the
 * terminal app wraps it at a "-" or "/", and a terminal can then neither click nor copy it
 * whole (macOS Terminal.app has no OSC 8 links). A link with a title keeps its title; the
 * brackets around its address go, since the line break already sets it apart.
 */
export function urlsOnTheirOwnLines(
  spans: MarkdownInlineSpan[],
  context?: Pick<MarkdownInlineContext, "tableCell">,
): MarkdownInlineSpan[] {
  if (context?.tableCell) return spans; // a table cell has no room for lines of its own
  const isUrl = (span: MarkdownInlineSpan | undefined) =>
    span?.link !== undefined && span.text === span.link.url && span.text.length >= LONG_URL;
  const out: MarkdownInlineSpan[] = [];
  let breakNext = false;
  spans.forEach((span, i) => {
    const bracket =
      span.link !== undefined &&
      ((span.text === "(" && isUrl(spans[i + 1])) || (span.text === ")" && isUrl(spans[i - 1])));
    if (bracket) return;
    if (isUrl(span)) {
      // End the line before it, without the space that led up to it.
      const last = out.at(-1);
      if (last) out[out.length - 1] = { ...last, text: `${last.text.trimEnd()}\n` };
      out.push(span);
      breakNext = true;
      return;
    }
    if (!breakNext) {
      out.push(span);
      return;
    }
    const text = span.text.trimStart();
    if (!text) return; // only the space after the URL
    out.push({ ...span, text: `\n${text}` });
    breakNext = false;
  });
  return out;
}
