/**
 * Images in the input (#53, ADR 0033): a file dragged onto the terminal (the terminal pastes its
 * path), or an image on the clipboard (Ctrl+V). Each becomes an "[Image N]" tag, like long pastes.
 */
import { spawn } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import { readFile, rm } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { basename, join } from "node:path";
import type { UploadedImage } from "@enzo/sdk";
import { createHostClipboard } from "@opentui/core";

/** Files the gateway reads (it checks the bytes; the name only decides whether to try). */
const IMAGE_FILE = /\.(png|jpe?g|gif|webp|bmp)$/i;
const TYPES: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
};
/** The most a clipboard image may be (the gateway's own limit). */
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

/** An image in the input: its tag stands for it until the message is sent. */
export interface Attachment {
  tag: string;
  image: UploadedImage;
}

/** What the input asks the app for: keep an image from a file or from the clipboard. */
export interface ImageAttacher {
  file(path: string): Promise<UploadedImage>;
  /** Null: the clipboard holds no image. */
  clipboard(): Promise<UploadedImage | null>;
}

/** The type an image file's name says it is. */
export function typeOf(path: string): string {
  const ext = path.toLowerCase().split(".").pop() ?? "";
  return TYPES[ext] ?? "application/octet-stream";
}

/**
 * The image files a paste names, or null when it is not only image paths. Terminals paste a
 * dragged file's path escaped as a shell word (`/Users/a/Screen\ Shot.png`), quoted
 * (`'/home/a/shot.png'`), or as a `file://` address; several files are separated by spaces.
 */
export function imagePaths(pasted: string, exists = isFile): string[] | null {
  // One file pasted as it is, spaces and all (some terminals do not escape them).
  const whole = fromWord(pasted.trim());
  if (IMAGE_FILE.test(whole) && exists(whole)) return [whole];
  const words = shellWords(pasted.trim());
  if (!words || words.length === 0) return null;
  const paths = words.map(fromWord);
  return paths.every((p) => IMAGE_FILE.test(p) && exists(p)) ? paths : null;
}

function isFile(path: string): boolean {
  try {
    return existsSync(path) && statSync(path).isFile();
  } catch {
    return false;
  }
}

function fromWord(word: string): string {
  let path = word;
  if (path.startsWith("file://")) {
    try {
      path = decodeURIComponent(new URL(path).pathname);
    } catch {
      return path;
    }
  }
  return path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
}

/** Shell-like words: quotes and backslash escapes. Null when a quote is left open. */
function shellWords(text: string): string[] | null {
  const words: string[] = [];
  let word = "";
  let quote: string | null = null;
  let has = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i] as string;
    if (quote) {
      if (c === quote) quote = null;
      else if (c === "\\" && quote === '"' && i + 1 < text.length) word += text[++i];
      else word += c;
    } else if (c === "'" || c === '"') {
      quote = c;
      has = true;
    } else if (c === "\\" && i + 1 < text.length) {
      word += text[++i];
      has = true;
    } else if (/\s/.test(c)) {
      if (has || word) words.push(word);
      word = "";
      has = false;
    } else {
      word += c;
      has = true;
    }
  }
  if (quote) return null;
  if (has || word) words.push(word);
  return words;
}

export interface ClipboardImage {
  bytes: Uint8Array;
  type: string;
}

const CLIPBOARD_TYPES = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "image/bmp",
] as const;

/**
 * An image from the system clipboard, or null when it holds none. OpenTUI reads it natively
 * (X11, Wayland, and AppKit on macOS); where that cannot, the usual tools are asked instead:
 * `osascript` on macOS, `wl-paste` or `xclip` on Linux.
 */
export async function readClipboardImage(): Promise<ClipboardImage | null> {
  const host = createHostClipboard({ timeoutMs: 3000, maxReadBytes: MAX_IMAGE_BYTES });
  try {
    const read = await host.read({ preferredTypes: [...CLIPBOARD_TYPES] });
    if (read.status === "read" && read.representation.mimeType.startsWith("image/")) {
      return { bytes: read.representation.bytes, type: read.representation.mimeType };
    }
    if (read.status === "empty") return null;
  } catch {
    // fall through to the platform's own tools
  } finally {
    await host.dispose().catch(() => {});
  }
  return platformClipboardImage();
}

async function platformClipboardImage(): Promise<ClipboardImage | null> {
  if (process.platform === "darwin") {
    const file = join(tmpdir(), `enzo-clipboard-${process.pid}-${Date.now()}.png`);
    const script = [
      `set f to open for access POSIX file ${JSON.stringify(file)} with write permission`,
      "try",
      "write (the clipboard as «class PNGf») to f",
      "end try",
      "close access f",
    ];
    try {
      await run(
        "osascript",
        script.flatMap((line) => ["-e", line]),
      );
      const bytes = new Uint8Array(await readFile(file));
      return bytes.length > 0 ? { bytes, type: "image/png" } : null;
    } catch {
      return null;
    } finally {
      await rm(file, { force: true });
    }
  }
  const tries: [string, string[]][] = [];
  if (process.env.WAYLAND_DISPLAY)
    tries.push(["wl-paste", ["--no-newline", "--type", "image/png"]]);
  if (process.env.DISPLAY)
    tries.push(["xclip", ["-selection", "clipboard", "-t", "image/png", "-o"]]);
  for (const [command, args] of tries) {
    try {
      const bytes = await run(command, args);
      if (bytes.length > 0) return { bytes, type: "image/png" };
    } catch {
      // not installed, or no image there
    }
  }
  return null;
}

/** A command's output, or a rejection if it fails, is missing, or takes too long. */
function run(command: string, args: string[]): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "ignore"] });
    const chunks: Buffer[] = [];
    let size = 0;
    const timer = setTimeout(() => child.kill(), 5000);
    child.stdout.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_IMAGE_BYTES) child.kill();
      else chunks.push(chunk);
    });
    child.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && size <= MAX_IMAGE_BYTES) resolve(new Uint8Array(Buffer.concat(chunks)));
      else reject(new Error(`${command} exited with ${code}`));
    });
  });
}

/** The next tag: "[Image 1]", then "[Image 2]", ... (numbers never repeat within a message). */
export function nextTag(earlier: readonly Attachment[]): string {
  const used = earlier.map((a) => Number(a.tag.match(/\d+/)?.[0] ?? 0));
  return `[Image ${Math.max(0, ...used) + 1}]`;
}

/** The images whose tags are still in the message, in the order the tags appear. */
export function attachedIn(text: string, attachments: readonly Attachment[]): Attachment[] {
  return attachments
    .filter((a) => text.includes(a.tag))
    .sort((a, b) => text.indexOf(a.tag) - text.indexOf(b.tag));
}

/** How a chip reads above the input: "[Image 1] shot.png 1920×1080". */
export function chipText(a: Attachment): string {
  const name = a.image.name ? ` ${a.image.name}` : "";
  return `${a.tag}${name} ${a.image.width}×${a.image.height}`;
}

/** Keeps a file's image with the gateway: what dragging a file onto the terminal does. */
export async function uploadFile(
  upload: (
    data: Uint8Array,
    options: { source: "file"; name: string; type: string },
  ) => Promise<UploadedImage>,
  path: string,
): Promise<UploadedImage> {
  const bytes = new Uint8Array(await readFile(path));
  return upload(bytes, { source: "file", name: basename(path), type: typeOf(path) });
}
