import { describe, expect, it } from "vitest";
import { parseSse, type SseMessage } from "./sse.ts";

const STREAM = [
  ": keep-alive comment",
  "event: text",
  'data: {"text":"héllo ✓"}',
  "",
  "event: done\r",
  "data: line one\r",
  "data: line two\r",
  "id: 7\r",
  "\r",
  "data: no event name",
  "",
  "",
].join("\n");

const EXPECTED: SseMessage[] = [
  { event: "text", data: '{"text":"héllo ✓"}' },
  { event: "done", data: "line one\nline two", id: "7" },
  { event: "message", data: "no event name", id: "7" },
];

function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(c);
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<SseMessage[]> {
  const out: SseMessage[] = [];
  for await (const m of parseSse(stream)) out.push(m);
  return out;
}

describe("parseSse", () => {
  it("parses events, comments, CRLF and multi-line data", async () => {
    expect(await collect(streamOf([new TextEncoder().encode(STREAM)]))).toEqual(EXPECTED);
  });

  it("gives the same result however the bytes are split (CRLF pairs, multi-byte chars)", async () => {
    const bytes = new TextEncoder().encode(STREAM);
    for (let cut = 1; cut < bytes.length; cut++) {
      const got = await collect(streamOf([bytes.slice(0, cut), bytes.slice(cut)]));
      expect(got, `split at byte ${cut}`).toEqual(EXPECTED);
    }
  });

  it("delivers byte-by-byte streams too", async () => {
    const bytes = new TextEncoder().encode(STREAM);
    const chunks = Array.from(bytes, (b) => new Uint8Array([b]));
    expect(await collect(streamOf(chunks))).toEqual(EXPECTED);
  });

  it("drops an unterminated last event instead of guessing", async () => {
    const got = await collect(streamOf([new TextEncoder().encode("event: text\ndata: partial")]));
    expect(got).toEqual([]);
  });
});
