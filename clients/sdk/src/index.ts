/** Typed client for the Enzo gateway. Every client surface (web, terminal) goes through here. */

export interface Health {
  status: "ok";
  version: string;
  env: string;
}

export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

export interface GatewayClientOptions {
  baseUrl: string;
  fetch?: typeof fetch;
}

export class GatewayClient {
  readonly #baseUrl: string;
  readonly #fetch: typeof fetch;

  constructor({ baseUrl, fetch: fetchImpl = globalThis.fetch }: GatewayClientOptions) {
    this.#baseUrl = baseUrl.replace(/\/+$/, "");
    // Browsers throw "Illegal invocation" if fetch runs with `this` bound to another object.
    this.#fetch = (input, init) => fetchImpl(input, init);
  }

  async health(signal?: AbortSignal): Promise<Health> {
    const res = await this.#fetch(`${this.#baseUrl}/healthz`, signal ? { signal } : {});
    if (!res.ok) throw new GatewayError(`healthz failed: HTTP ${res.status}`, res.status);
    const body: unknown = await res.json();
    if (!isHealth(body)) throw new GatewayError("healthz returned an unexpected body");
    return body;
  }
}

function isHealth(v: unknown): v is Health {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return o.status === "ok" && typeof o.version === "string" && typeof o.env === "string";
}
