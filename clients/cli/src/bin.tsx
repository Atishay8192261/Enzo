#!/usr/bin/env -S npx tsx
import { GatewayClient } from "@enzo/sdk";
import { render } from "ink";
import { App } from "./App.tsx";

const baseUrl = process.env.ENZO_GATEWAY_URL ?? "http://127.0.0.1:8787";
render(<App client={new GatewayClient({ baseUrl })} />);
