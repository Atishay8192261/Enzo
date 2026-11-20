import {
  createClipboard,
  createHostClipboard,
  createRendererClipboardAdapter,
  type RendererClipboardBoundary,
} from "@opentui/core";

/** Puts text on the clipboard. Resolves false when nothing could take it. */
export type Copy = (text: string) => Promise<boolean>;

/**
 * The system clipboard where there is one (pbcopy, wl-copy, xclip), and the terminal's own
 * (OSC 52) too, so copying also works over SSH (#35).
 */
export function clipboard(renderer: RendererClipboardBoundary): Copy {
  const service = createClipboard({
    host: createHostClipboard(),
    terminal: createRendererClipboardAdapter(renderer),
  });
  return async (text) => {
    // OpenTUI refuses NUL before trying any destination.
    const result = await service.writeText(text.replaceAll("\0", ""), {
      destination: "all-available",
      selection: "clipboard",
    });
    return result.host.status === "written" || result.terminal.status === "attempted";
  };
}

/** The text of the last fenced code block in a reply, or null when it has none. */
export function lastCodeBlock(markdown: string): string | null {
  const blocks = [...markdown.matchAll(/^(`{3,}|~{3,})[^\n]*\n([\s\S]*?)^\1[ \t]*$/gm)];
  const last = blocks.at(-1);
  return last ? (last[2] ?? "").replace(/\n$/, "") : null;
}
