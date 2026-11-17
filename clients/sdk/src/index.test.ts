import { afterEach, describe, expect, it, vi } from "vitest";
import { GatewayClient, GatewayError } from "./index.ts";

const fakeFetch = (status: number, body: unknown): typeof fetch =>
  (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;

describe("GatewayClient.health", () => {
  it("returns the parsed health payload", async () => {
    const client = new GatewayClient({
      baseUrl: "http://x/",
      fetch: fakeFetch(200, { status: "ok", version: "0.1.0", env: "test" }),
    });
    await expect(client.health()).resolves.toEqual({ status: "ok", version: "0.1.0", env: "test" });
  });

  it("fails loudly on a non-2xx status", async () => {
    const client = new GatewayClient({ baseUrl: "http://x", fetch: fakeFetch(503, {}) });
    await expect(client.health()).rejects.toThrow(GatewayError);
  });

  it("rejects a malformed body instead of trusting it", async () => {
    const client = new GatewayClient({
      baseUrl: "http://x",
      fetch: fakeFetch(200, { status: "ok" }),
    });
    await expect(client.health()).rejects.toThrow(/unexpected body/);
  });
});

describe("GatewayClient default fetch", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("never calls fetch with `this` bound to the client (browser 'Illegal invocation')", async () => {
    vi.stubGlobal("fetch", function (this: unknown) {
      if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
      return Promise.resolve(Response.json({ status: "ok", version: "1", env: "test" }));
    });
    await expect(new GatewayClient({ baseUrl: "http://x" }).health()).resolves.toMatchObject({
      status: "ok",
    });
  });
});
