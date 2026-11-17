#!/usr/bin/env bun
import { GatewayClient } from "@enzo/sdk";
import { createCliRenderer } from "@opentui/core";
import { createRoot } from "@opentui/react";
import { App } from "./App.tsx";
import { fileMemory } from "./memory.ts";

const baseUrl = process.env.ENZO_GATEWAY_URL ?? "http://127.0.0.1:8787";
const token = process.env.ENZO_API_TOKEN;
const client = new GatewayClient({ baseUrl, ...(token ? { token } : {}) });
const renderer = await createCliRenderer({ exitOnCtrlC: true });
const quit = () => {
  renderer.destroy();
  process.exit(0);
};
createRoot(renderer).render(<App client={client} memory={fileMemory()} onQuit={quit} />);
