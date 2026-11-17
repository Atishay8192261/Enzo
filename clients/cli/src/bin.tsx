#!/usr/bin/env -S npx tsx
import { GatewayClient } from "@enzo/sdk";
import { render } from "ink";
import { App } from "./App.tsx";
import { fileMemory } from "./memory.ts";

const baseUrl = process.env.ENZO_GATEWAY_URL ?? "http://127.0.0.1:8787";
const token = process.env.ENZO_API_TOKEN;
const client = new GatewayClient({ baseUrl, ...(token ? { token } : {}) });
render(<App client={client} memory={fileMemory()} />);
