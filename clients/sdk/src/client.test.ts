import { describe, expect, it } from "vitest";
import { GatewayClient, GatewayError, GatewayUnreachableError } from "./client.ts";

interface Seen {
  url: string;
  init: RequestInit;
}

function fakeFetch(
  respond: (url: string, init: RequestInit) => Response,
  seen: Seen[] = [],
): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.push({ url: String(input), init: init ?? {} });
    return respond(String(input), init ?? {});
  }) as typeof fetch;
}

const sse = (body: string) =>
  new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });

describe("GatewayClient", () => {
  it("streams a turn as typed events and sends the token", async () => {
    const seen: Seen[] = [];
    const client = new GatewayClient({
      baseUrl: "http://enzo/",
      token: "s3cret",
      fetch: fakeFetch(
        () =>
          sse(
            'event: text\ndata: {"text":"hi"}\n\nevent: done\ndata: {"stop":"final_answer","text":"hi","steps":1,"cost_usd":0.001,"brain":"b","model":"m","failure":null,"plan":false,"usage":{}}\n\n',
          ),
        seen,
      ),
    });
    const events = [];
    for await (const e of client.send("c-1", "hello", { timezone: "UTC", device: "web" }))
      events.push(e);
    expect(events.map((e) => e.type)).toEqual(["text", "done"]);
    expect(seen[0]?.url).toBe("http://enzo/conversations/c-1/turns");
    const headers = seen[0]?.init.headers as Record<string, string> | undefined;
    expect(headers?.authorization).toBe("Bearer s3cret");
    expect(JSON.parse(String(seen[0]?.init.body))).toEqual({
      message: "hello",
      timezone: "UTC",
      device: "web",
    });
  });

  it("reports HTTP errors with the server's reason before any streaming", async () => {
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(() =>
        Response.json({ detail: "a turn is already running" }, { status: 409 }),
      ),
    });
    const stream = client.send("c-1", "hello");
    await expect(stream.next()).rejects.toMatchObject({
      name: "GatewayError",
      status: 409,
      message: expect.stringContaining("a turn is already running"),
    });
  });

  it("says plainly when the gateway can't be reached at all (#44)", async () => {
    const refused = new TypeError("Unable to connect. Is the computer able to access the url?");
    const client = new GatewayClient({
      baseUrl: "http://127.0.0.1:8787/",
      fetch: async () => {
        throw refused;
      },
    });
    const failure = await client.health().catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(GatewayUnreachableError);
    expect(failure).toBeInstanceOf(GatewayError); // callers that catch GatewayError still do
    expect(failure).toMatchObject({
      message: "Can't reach Enzo at 127.0.0.1:8787",
      url: "http://127.0.0.1:8787",
      cause: refused,
    });
    await expect(client.send("c-1", "hi").next()).rejects.toBeInstanceOf(GatewayUnreachableError);
  });

  it("leaves a request the caller stopped as it is, not as a connection problem", async () => {
    const stop = new AbortController();
    const aborted = new DOMException("The operation was aborted.", "AbortError");
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: async () => {
        stop.abort();
        throw aborted;
      },
    });
    await expect(client.health(stop.signal)).rejects.toBe(aborted);
  });

  it("handles empty 204 replies", async () => {
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(() => new Response(null, { status: 204 })),
    });
    await expect(client.decide("c", "appr_1", true)).resolves.toBeUndefined();
    await expect(client.chooseBrain("c", null)).resolves.toBeUndefined();
  });

  it("escapes ids in paths", async () => {
    const seen: Seen[] = [];
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(() => new Response(null, { status: 204 }), seen),
    });
    await client.decide("c/../x", "a b", false);
    expect(seen[0]?.url).toBe("http://enzo/conversations/c%2F..%2Fx/approvals/a%20b");
  });

  it("connects, checks and disconnects provider accounts", async () => {
    const seen: Seen[] = [];
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(
        (url) =>
          url.endsWith("/check")
            ? Response.json({ ok: false, detail: "xai rejected this key" })
            : new Response(null, { status: 204 }),
        seen,
      ),
    });
    await client.connect("xai", "fake-key-000000");
    await expect(client.checkConnection("xai")).resolves.toEqual({
      ok: false,
      detail: "xai rejected this key",
    });
    await client.disconnect("x/y");
    expect(seen.map((s) => `${s.init.method} ${s.url}`)).toEqual([
      "PUT http://enzo/connections/xai",
      "POST http://enzo/connections/xai/check",
      "DELETE http://enzo/connections/x%2Fy",
    ]);
    expect(JSON.parse(String(seen[0]?.init.body))).toEqual({ api_key: "fake-key-000000" });
  });

  it("reports a rejected key with the server's reason and never the key", async () => {
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(() =>
        Response.json({ detail: "anthropic rejected this key" }, { status: 422 }),
      ),
    });
    const error = await client.connect("anthropic", "fake-key-000000").catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 422 });
    expect(String((error as Error).message)).toContain("anthropic rejected this key");
    expect(String((error as Error).message)).not.toContain("fake-key");
  });

  it("turns validation errors into field: reason", async () => {
    const detail = [
      { type: "string_too_short", loc: ["body", "api_key"], msg: "too short" },
      { type: "missing", loc: ["body"], msg: "Field required" },
    ];
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(() => Response.json({ detail }, { status: 422 })),
    });
    await expect(client.connect("xai", "x")).rejects.toThrow(
      "/connections/xai failed: HTTP 422: api_key: too short; Field required",
    );
  });

  it("keeps GatewayError exported for callers", () => {
    expect(new GatewayError("x", 500).status).toBe(500);
  });
});
