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

  it("uploads an image as raw bytes, its name in a header, and sends its id", async () => {
    const seen: Seen[] = [];
    const kept = { id: "img_0123456789abcdef", media_type: "image/png", width: 2, height: 1 };
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      token: "t",
      fetch: fakeFetch(
        (url) =>
          url.endsWith("/images")
            ? Response.json(kept, { status: 201 })
            : sse('event: error\ndata: {"message":"x"}\n\n'),
        seen,
      ),
    });
    const bytes = new Uint8Array([137, 80, 78, 71]);
    const got = await client.uploadImage(bytes, {
      source: "file",
      name: "Screen Shot 1.png",
      type: "image/png",
    });
    expect(got.id).toBe(kept.id);
    expect(seen[0]?.url).toBe("http://enzo/images");
    expect(seen[0]?.init.body).toBe(bytes);
    expect(seen[0]?.init.headers).toEqual({
      "content-type": "image/png",
      "x-enzo-image-source": "file",
      "x-enzo-image-name": "Screen%20Shot%201.png",
      authorization: "Bearer t",
    });
    for await (const _ of client.send("c-1", "what is it?", { images: [got.id] })) {
      // drain
    }
    expect(JSON.parse(String(seen[1]?.init.body))).toEqual({
      message: "what is it?",
      images: [kept.id],
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

  it("lists, renames and trashes chats, and can start one with a model (#38)", async () => {
    const seen: Seen[] = [];
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch((_url, init) => {
        if (init.method === "GET") return Response.json([{ id: "c1", title: "Plan" }]);
        if (init.method === "POST") return Response.json({ id: "c2", brain: "fast" });
        return new Response(null, { status: 204 });
      }, seen),
    });
    expect(await client.conversations(5)).toEqual([{ id: "c1", title: "Plan" }]);
    await client.renameConversation("c 1", "Weekly plan");
    await client.deleteConversation("c1");
    await client.createConversation("main", undefined, "fast");
    await client.createConversation("main");
    expect(seen.map((x) => `${x.init.method} ${x.url} ${x.init.body ?? ""}`)).toEqual([
      "GET http://enzo/conversations?limit=5 ",
      'PATCH http://enzo/conversations/c%201 {"title":"Weekly plan"}',
      "DELETE http://enzo/conversations/c1 ",
      'POST http://enzo/conversations {"kind":"main","brain":"fast"}',
      'POST http://enzo/conversations {"kind":"main"}',
    ]);
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

  it("sets up a Google client and signs in with Google", async () => {
    const seen: Seen[] = [];
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch((url, init) => {
        if (init.method === "PUT") return new Response(null, { status: 204 });
        if (init.method === "POST") {
          return Response.json({ login_id: "L1", url: "http://127.0.0.1:8787/auth/google/L1" });
        }
        return Response.json({ status: "connected", message: `seen ${url}` });
      }, seen),
    });
    await client.setGoogleClient('{"installed":{}}');
    const started = await client.startGoogleSignIn("read");
    expect(started.login_id).toBe("L1");
    await expect(client.googleSignIn("L/1")).resolves.toEqual({
      status: "connected",
      message: "seen http://enzo/connections/google/sign-in/L%2F1",
    });
    expect(seen.map((s) => `${s.init.method} ${s.url}`)).toEqual([
      "PUT http://enzo/connections/google/client",
      "POST http://enzo/connections/google/sign-in",
      "GET http://enzo/connections/google/sign-in/L%2F1",
    ]);
    expect(JSON.parse(String(seen[0]?.init.body))).toEqual({ client: '{"installed":{}}' });
    expect(JSON.parse(String(seen[1]?.init.body))).toEqual({ access: "read" });
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

describe("goals (spec §6.4)", () => {
  it("lists the open ones, or the closed ones", async () => {
    const seen: Seen[] = [];
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(
        () => Response.json({ goals: [{ id: 1, title: "Renew my passport" }], file: null }),
        seen,
      ),
    });
    expect((await client.goals()).goals.map((g) => g.title)).toEqual(["Renew my passport"]);
    await client.goals("closed");
    expect(seen.map((s) => `${s.init.method} ${s.url}`)).toEqual([
      "GET http://enzo/goals?which=open",
      "GET http://enzo/goals?which=closed",
    ]);
  });
});

describe("reminders (spec §9.3)", () => {
  it("lists and cancels them, and reads a chat's delivered ones", async () => {
    const seen: Seen[] = [];
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch((url, init) => {
        if (init.method === "DELETE") return new Response(null, { status: 204 });
        if (url.endsWith("/scheduled") && url.includes("/conversations/")) {
          return Response.json([{ text: "Tea", after: 2, late: false }]);
        }
        return Response.json({ scheduled: [{ id: 3, text: "Call mom" }] });
      }, seen),
    });
    expect((await client.scheduled()).map((r) => r.text)).toEqual(["Call mom"]);
    await client.cancelScheduled(3);
    expect((await client.conversationScheduled("c 1")).map((r) => r.text)).toEqual(["Tea"]);
    expect(seen.map((s) => `${s.init.method} ${s.url}`)).toEqual([
      "GET http://enzo/scheduled",
      "DELETE http://enzo/scheduled/3",
      "GET http://enzo/conversations/c%201/scheduled",
    ]);
  });

  it("streams live events, skipping kinds it does not know and broken data", async () => {
    const client = new GatewayClient({
      baseUrl: "http://enzo",
      fetch: fakeFetch(() =>
        sse(
          'event: something_new\ndata: {"x":1}\n\n' +
            "event: reminder\ndata: {not json\n\n" +
            'event: reminder\ndata: {"reminder":3,"text":"Call mom","words":"Call mom","late":false,"conversation_id":"c1","next":null}\n\n' +
            'event: task\ndata: {"task":5,"outcome":"reported","words":"Weather: sunny","chat_id":"t1","conversation_id":"c1","stopped":false}\n\n',
        ),
      ),
    });
    const got = [];
    for await (const e of client.events()) got.push(e);
    expect(got).toEqual([
      {
        kind: "reminder",
        reminder: 3,
        text: "Call mom",
        words: "Call mom",
        late: false,
        conversation_id: "c1",
        next: null,
      },
      {
        kind: "task",
        task: 5,
        outcome: "reported",
        words: "Weather: sunny",
        chat_id: "t1",
        conversation_id: "c1",
        stopped: false,
      },
    ]);
  });
});
