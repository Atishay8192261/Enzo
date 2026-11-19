import { type AccountsClient, CHATGPT_USAGE_URL, methodName, signInWithChatGPT } from "@enzo/chat";
import type { ProviderConnection } from "@enzo/sdk";
import { type FormEvent, useCallback, useEffect, useState } from "react";

interface Notice {
  text: string;
  error?: boolean;
  /** A link the notice points to, e.g. the sign-in page when no tab opened. */
  link?: { href: string; label: string };
}

/** How each company writes its own name. */
const NAMES: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  xai: "xAI",
  moonshot: "Moonshot",
  tavily: "Tavily",
  github: "GitHub",
};

/** A tab opened inside the click (so it is not blocked), cut off from this page. */
function blankTab(): Window | null {
  const tab = window.open("about:blank", "_blank");
  if (tab) tab.opener = null;
  return tab;
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

  const continueWithChatGPT = (newAccount = false) => {
    const tab = blankTab();
    void run("openai", async () => {
      const result = await signInWithChatGPT(client, {
        newAccount,
        open: (url) => {
          if (tab) tab.location.href = url;
          else throw new Error("no tab");
        },
        waiting: (url) =>
          setNotice({
            text: "Finish signing in with ChatGPT in the new tab.",
            link: { href: url, label: "Open the sign-in page" },
          }),
      });
      return result.ok
        ? {
            text: result.text,
            link: { href: result.usageUrl ?? CHATGPT_USAGE_URL, label: "Manage usage" },
          }
        : { text: result.text, error: true };
    });
  };

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
      <p className="muted">
        Enzo runs on your own model accounts: sign in with ChatGPT, or paste an API key. Keys are
        checked, then kept in your computer's vault.
      </p>
      {notice && (
        <p role={notice.error ? "alert" : "status"}>
          {notice.text}
          {notice.link && (
            <>
              {" "}
              <a href={notice.link.href} target="_blank" rel="noopener noreferrer">
                {notice.link.label}
              </a>
            </>
          )}
        </p>
      )}
      {rows === null ? (
        <p role="status">Loading accounts…</p>
      ) : (
        <ul>
          {rows.map((c) => (
            <li key={c.provider} className={c.connected ? "account on" : "account"}>
              <div className="account-head">
                <strong>{NAMES[c.provider] ?? c.provider}</strong>
                <span className="pill">{c.connected ? "Connected" : "Not connected"}</span>
              </div>
              <small>{c.models.length > 0 ? c.models.join(", ") : c.purpose}</small>
              {c.connected ? (
                <span className="account-state">
                  {" "}
                  Connected ({methodName(c.method)}
                  {c.label ? `, ${c.label}` : ""}
                  {c.connected_at ? `, since ${c.connected_at.slice(0, 10)}` : ""}){" "}
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() =>
                      void run(c.provider, async () => {
                        const r = await client.checkConnection(c.provider);
                        return r.ok
                          ? { text: `${c.provider}: the connection works.` }
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
                        const out = await client.disconnect(c.provider);
                        if (out.remote_sign_out === "confirmed") {
                          return { text: "Signed out of ChatGPT." };
                        }
                        if (out.remote_sign_out === "not_confirmed") {
                          return {
                            text: "Signed out here, but OpenAI did not confirm it. To be sure, remove Enzo in your ChatGPT settings.",
                            error: true,
                            link: { href: CHATGPT_USAGE_URL, label: "ChatGPT settings" },
                          };
                        }
                        return { text: `Disconnected ${c.provider}. Its key was deleted.` };
                      })
                    }
                  >
                    {c.method === "chatgpt" ? "Sign out" : "Disconnect"}
                  </button>
                </span>
              ) : (
                <>
                  {c.methods.includes("chatgpt") && (
                    <button
                      type="button"
                      className="chatgpt"
                      disabled={busy !== null}
                      onClick={() => continueWithChatGPT()}
                    >
                      Continue with ChatGPT
                    </button>
                  )}
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
                </>
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
