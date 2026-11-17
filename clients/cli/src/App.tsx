import type { GatewayClient, Health } from "@enzo/sdk";
import { Box, Text } from "ink";
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
    <Box flexDirection="column">
      <Text bold>Enzo</Text>
      {state.kind === "loading" && <Text dimColor>Connecting to gateway…</Text>}
      {state.kind === "up" && (
        <Text color="green">
          Gateway ok · v{state.health.version} · {state.health.env}
        </Text>
      )}
      {state.kind === "down" && <Text color="red">Gateway unreachable: {state.error}</Text>}
    </Box>
  );
}
