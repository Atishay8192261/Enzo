import type { GatewayClient, Health } from "@enzo/sdk";
import { useEffect, useState } from "react";

type State = { kind: "loading" } | { kind: "up"; health: Health } | { kind: "down"; error: string };

export function App({ client }: { client: Pick<GatewayClient, "health"> }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    client
      .health(ctrl.signal)
      .then((health) => setState({ kind: "up", health }))
      .catch((e: unknown) => {
        if (!ctrl.signal.aborted) setState({ kind: "down", error: String(e) });
      });
    return () => ctrl.abort();
  }, [client]);

  return (
    <main>
      <h1>Enzo</h1>
      <p role="status">
        {state.kind === "loading" && "Connecting to gateway…"}
        {state.kind === "up" && `Gateway ok · v${state.health.version} · ${state.health.env}`}
        {state.kind === "down" && `Gateway unreachable: ${state.error}`}
      </p>
    </main>
  );
}
