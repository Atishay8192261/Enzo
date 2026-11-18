import {
  type AccountsClient,
  type ChatClient,
  type ConversationMemory,
  describeApproval,
  formatCost,
  useChat,
} from "@enzo/chat";
import { type FormEvent, useState } from "react";
import { Accounts } from "./Accounts.tsx";
import { ModelPicker } from "./ModelPicker.tsx";

const KEY = "enzo.conversation";

/** Browser storage can throw (private mode, blocked site data): never fatal. */
const browserMemory: ConversationMemory = {
  get() {
    try {
      return localStorage.getItem(KEY);
    } catch {
      return null;
    }
  },
  set(id) {
    try {
      localStorage.setItem(KEY, id);
    } catch {
      // the next visit starts a new conversation
    }
  },
};

export function App({ client }: { client: ChatClient & AccountsClient }) {
  const { state, send, decide, chooseBrain, refreshBrains } = useChat(client, browserMemory, "web");
  const [text, setText] = useState("");
  const [accountsOpen, setAccountsOpen] = useState(false);
  const busy = state.status !== "ready";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const value = text.trim();
    if (!value || busy) return;
    setText("");
    void send(value);
  };

  return (
    <main className="chat">
      <header>
        <h1>Enzo</h1>
        <ModelPicker
          brains={state.brains}
          value={state.brain}
          disabled={busy}
          onChange={(choice) => void chooseBrain(choice)}
        />
      </header>

      <details
        className="accounts-toggle"
        open={accountsOpen}
        onToggle={(e) => setAccountsOpen(e.currentTarget.open)}
      >
        <summary>Accounts</summary>
        <Accounts client={client} onChange={() => void refreshBrains()} />
      </details>

      {state.status === "connecting" && <p role="status">Connecting…</p>}
      {state.status === "offline" && <p role="alert">Gateway unreachable: {state.error}</p>}

      <ol className="items" aria-label="Conversation">
        {state.items.map((item, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
          <li key={i} className={item.role}>
            {item.failure === "not_connected" ? (
              <>
                No model account is connected yet.{" "}
                <button type="button" onClick={() => setAccountsOpen(true)}>
                  Open Accounts
                </button>{" "}
                to connect Anthropic, OpenAI, xAI or Moonshot.
              </>
            ) : (
              item.text
            )}
            {item.by && <small className="by">{item.by}</small>}
          </li>
        ))}
        {state.draft && <li className="assistant draft">{state.draft}</li>}
        {state.working > 0 && (
          <li className="notice" role="status">
            Working…
          </li>
        )}
      </ol>

      {state.approvals.map((a) => (
        <section key={a.id} className="approval" aria-label="Approval needed">
          <h2>Approve this action?</h2>
          <p>
            <strong>{describeApproval(a)}</strong>
          </p>
          <p>Exact details:</p>
          <pre>{JSON.stringify(a.arguments, null, 2)}</pre>
          <button type="button" onClick={() => void decide(a.id, true)}>
            Approve
          </button>
          <button type="button" onClick={() => void decide(a.id, false)}>
            Decline
          </button>
        </section>
      ))}

      {state.error && state.status !== "offline" && <p role="alert">{state.error}</p>}

      <form onSubmit={submit}>
        <input
          aria-label="Message"
          value={text}
          placeholder={busy ? "Enzo is working…" : "Message Enzo"}
          onChange={(e) => setText(e.target.value)}
          disabled={state.status === "offline"}
        />
        <button type="submit" disabled={busy || !text.trim()}>
          Send
        </button>
        <small>{formatCost(state.lastCost)}</small>
      </form>
    </main>
  );
}
