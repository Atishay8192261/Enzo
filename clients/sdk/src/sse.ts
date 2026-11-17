/**
 * Server-Sent Events parser for POST streams (the browser's EventSource only does GET).
 * Follows the WHATWG event-stream rules: any line ending, multi-line data, comments, and
 * chunks that split a line, a CRLF pair or a multi-byte character.
 */

export interface SseMessage {
  event: string;
  data: string;
  id?: string;
}

export async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let data: string[] = [];
  let id: string | undefined;

  const take = (line: string): SseMessage | undefined => {
    if (line === "") {
      const message =
        data.length > 0
          ? { event: event || "message", data: data.join("\n"), ...(id ? { id } : {}) }
          : undefined;
      event = "";
      data = [];
      return message;
    }
    if (line.startsWith(":")) return undefined; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id" && !value.includes("\0")) id = value;
    return undefined;
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      for (;;) {
        const end = buffer.search(/\r\n|\r|\n/);
        if (end === -1) break;
        // A trailing "\r" may be the first half of "\r\n": wait for the next chunk.
        if (buffer[end] === "\r" && end === buffer.length - 1 && !done) break;
        const width = buffer.startsWith("\r\n", end) ? 2 : 1;
        const message = take(buffer.slice(0, end));
        buffer = buffer.slice(end + width);
        if (message) yield message;
      }
      if (done) return; // an unterminated final event is incomplete: dropped, per the spec
    }
  } finally {
    reader.releaseLock();
  }
}
