import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { App } from "./App.tsx";

const settle = () => new Promise((r) => setTimeout(r, 20));

describe("CLI App", () => {
  it("shows gateway version when healthy", async () => {
    const client = {
      health: async () => ({ status: "ok", version: "9.9.9", env: "test" }) as const,
    };
    const { lastFrame } = render(<App client={client} />);
    await settle();
    expect(lastFrame()).toContain("Gateway ok · v9.9.9 · test");
  });

  it("says so plainly when the gateway is down", async () => {
    const client = { health: async () => Promise.reject(new Error("ECONNREFUSED")) };
    const { lastFrame } = render(<App client={client} />);
    await settle();
    expect(lastFrame()).toContain("Gateway unreachable: Error: ECONNREFUSED");
  });
});
