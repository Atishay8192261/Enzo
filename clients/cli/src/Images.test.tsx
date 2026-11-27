import { afterAll, describe, expect, it, mock } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { imagePaths } from "./images.ts";
import { CANT_SEE } from "./Prompt.tsx";
import {
  BRAINS,
  fakeClient,
  paste,
  press,
  screen,
  sender,
  settle,
  type,
  until,
} from "./testing.tsx";

const dir = mkdtempSync(join(tmpdir(), "enzo-images-"));
const shot = join(dir, "Screen Shot 1.png");
writeFileSync(shot, new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

const SEEING = BRAINS.map((b) => (b.name === "claude-sonnet" ? { ...b, vision: true } : b));

describe("dragged image paths", () => {
  it("reads the ways terminals paste a dropped file, and nothing else", () => {
    const exists = (p: string) => p === "/a/Screen Shot 1.png" || p === "/b/x.JPG";
    expect(imagePaths("/a/Screen\\ Shot\\ 1.png ", exists)).toEqual(["/a/Screen Shot 1.png"]);
    expect(imagePaths("'/a/Screen Shot 1.png'", exists)).toEqual(["/a/Screen Shot 1.png"]);
    expect(imagePaths("file:///a/Screen%20Shot%201.png", exists)).toEqual(["/a/Screen Shot 1.png"]);
    expect(imagePaths("/a/Screen\\ Shot\\ 1.png /b/x.JPG", exists)).toEqual([
      "/a/Screen Shot 1.png",
      "/b/x.JPG",
    ]);
    expect(imagePaths("look at /b/x.JPG", exists)).toBeNull(); // words, not only paths
    expect(imagePaths("/b/missing.png", exists)).toBeNull();
    expect(imagePaths("/b/notes.txt", () => true)).toBeNull();
    expect(imagePaths("", exists)).toBeNull();
  });
});

describe("images in the terminal (#53)", () => {
  it("a dragged file becomes a chip and a tag, and goes with the message by id", async () => {
    const send = sender();
    const client = fakeClient({ send, brains: async () => SEEING });
    await using s = await screen(client);
    await paste(s, shot.replaceAll(" ", "\\ "));
    const frame = await until(s, "▣ [Image 1] Screen Shot 1.png 1456×819");
    expect(frame).toContain("❯ [Image 1]");
    expect(frame).not.toContain(CANT_SEE);
    expect(client.uploadImage).toHaveBeenCalledTimes(1);
    const [, options] = (client.uploadImage as ReturnType<typeof mock>).mock.calls[0] ?? [];
    expect(options).toEqual({ source: "file", name: "Screen Shot 1.png", type: "image/png" });
    await type(s, "what does this error mean?");
    await press(s, "RETURN");
    await settle(s, 100);
    const [, text, context] = send.mock.calls[0] ?? [];
    expect(text).toBe("[Image 1] what does this error mean?");
    expect(context).toMatchObject({ images: ["img_0123456789abcdef"] });
    expect(await until(s, "[Image 1] what does this error mean?")).not.toContain("▣");
  });

  it("Ctrl+V pastes the clipboard's image, and says so when there is none", async () => {
    const send = sender();
    let clip: { bytes: Uint8Array; type: string } | null = null;
    const client = fakeClient({ send, brains: async () => SEEING });
    await using s = await screen(client, { readClipboard: async () => clip });
    await press(s, "v", { ctrl: true });
    await until(s, "The clipboard has no image to paste.");
    clip = { bytes: new Uint8Array([1, 2, 3]), type: "image/png" };
    await press(s, "v", { ctrl: true });
    await until(s, "▣ [Image 1]");
    const [data, options] = (client.uploadImage as ReturnType<typeof mock>).mock.calls[0] ?? [];
    expect(data).toBe(clip.bytes);
    expect(options).toEqual({ source: "pasted", type: "image/png" });
  });

  it("deleting the tag removes the image, and a model that cannot see is named first", async () => {
    const send = sender();
    await using s = await screen(fakeClient({ send })); // no model that sees
    await paste(s, shot);
    const frame = await until(s, "▣ [Image 1]");
    expect(frame).toContain(CANT_SEE);
    for (let i = 0; i < "[Image 1] ".length; i++) await press(s, "BACKSPACE");
    await type(s, "never mind");
    await press(s, "RETURN");
    await settle(s, 100);
    const [, text, context] = send.mock.calls[0] ?? [];
    expect(text).toBe("never mind");
    expect(context).not.toHaveProperty("images");
  });

  it("an image the gateway refuses is said plainly and nothing is attached", async () => {
    const uploadImage = mock(async () => {
      throw new Error("/images failed: HTTP 415: That file isn't an image Enzo can read.");
    });
    await using s = await screen(fakeClient({ uploadImage }));
    await paste(s, shot);
    const frame = await until(s, "Couldn't attach Screen Shot 1.png: That file isn't an image");
    expect(frame).not.toContain("[Image 1]");
  });
});
