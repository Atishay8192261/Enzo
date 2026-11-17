import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const gateway = process.env.ENZO_GATEWAY_URL ?? "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react()],
  server: {
    // Same-origin in dev: /api/* is forwarded to the gateway, so no CORS is needed.
    proxy: { "/api": { target: gateway, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  test: { environment: "jsdom" },
});
