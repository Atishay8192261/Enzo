import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App.tsx";

describe("App", () => {
  it("shows gateway version when healthy", async () => {
    const client = {
      health: async () => ({ status: "ok", version: "9.9.9", env: "test" }) as const,
    };
    render(<App client={client} />);
    expect(await screen.findByText(/Gateway ok · v9\.9\.9 · test/)).toBeDefined();
  });

  it("says so plainly when the gateway is down", async () => {
    const client = { health: async () => Promise.reject(new Error("ECONNREFUSED")) };
    render(<App client={client} />);
    expect(await screen.findByText(/Gateway unreachable: Error: ECONNREFUSED/)).toBeDefined();
  });
});
