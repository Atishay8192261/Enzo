import { type AccountsClient, methodName } from "@enzo/chat";
import type { ProviderConnection } from "@enzo/sdk";
import { type FormEvent, useCallback, useEffect, useState } from "react";

interface Notice {
  text: string;
  error?: boolean;
}

const failure = (e: unknown): Notice => ({
  text: e instanceof Error ? e.message : String(e),
  error: true,
});

/** Connect your own provider accounts. Keys go straight to the gateway's vault, never back. */
export function Accounts({
  client,
  onChange,
}: {
  client: AccountsClient;
  /** Called after an account action succeeds, so the app can reload which models are usable. */
  onChange?: () => void;
}) {
  const [rows, setRows] = useState<ProviderConnection[] | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRows(await client.connections());
    } catch (e: unknown) {
      setNotice(failure(e));
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (provider: string, action: () => Promise<Notice>) => {
    setBusy(provider);
    try {
      setNotice(await action());
      onChange?.();
    } catch (e: unknown) {
      setNotice(failure(e));
    } finally {
      setBusy(null);
      await refresh();
    }
  };

  return (
    <section className="accounts" aria-label="Accounts">
      <h2>Your accounts</h2>
      <p>Enzo uses your own model accounts. Keys are checked, then kept in the vault.</p>
      {notice && <p role={notice.error ? "alert" : "status"}>{notice.text}</p>}
      {rows === null ? (
        <p role="status">Loading accounts…</p>
      ) : (
        <ul>
          {rows.map((c) => (
            <li key={c.provider}>
              <strong>{c.provider}</strong> <small>{c.models.join(", ")}</small>
              {c.connected ? (
                <span>
                  {" "}
                  Connected ({methodName(c.method)}
                  {c.connected_at ? `, since ${c.connected_at.slice(0, 10)}` : ""}){" "}
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() =>
                      void run(c.provider, async () => {
                        const r = await client.checkConnection(c.provider);
                        return r.ok
                          ? { text: `${c.provider}: the key works.` }
                          : { text: `${c.provider}: ${r.detail}`, error: true };
                      })
                    }
                  >
                    Check
                  </button>
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() =>
                      void run(c.provider, async () => {
                        await client.disconnect(c.provider);
                        return { text: `Disconnected ${c.provider}. Its key was deleted.` };
                      })
                    }
                  >
                    Disconnect
                  </button>
                </span>
              ) : (
                <KeyForm
                  provider={c.provider}
                  disabled={busy !== null}
                  onSubmit={(key) =>
                    run(c.provider, async () => {
                      await client.connect(c.provider, key);
                      return { text: `Connected ${c.provider}.` };
                    })
                  }
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function KeyForm({
  provider,
  disabled,
  onSubmit,
}: {
  provider: string;
  disabled: boolean;
  onSubmit: (key: string) => Promise<void>;
}) {
  const [key, setKey] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const value = key.trim();
    setKey(""); // out of the page before the request even starts
    if (value) void onSubmit(value);
  };
  return (
    <form onSubmit={submit}>
      <input
        type="password"
        aria-label={`${provider} API key`}
        placeholder="Paste API key"
        autoComplete="off"
        spellCheck={false}
        value={key}
        onChange={(e) => setKey(e.target.value)}
        disabled={disabled}
      />
      <button type="submit" disabled={disabled || !key.trim()}>
        Connect
      </button>
    </form>
  );
}
