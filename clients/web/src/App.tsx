import { type ChatClient, type ConversationMemory, formatCost, useChat } from "@enzo/chat";
import { type FormEvent, useState } from "react";

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

export function App({ client }: { client: ChatClient }) {
  const { state, send, decide, chooseBrain } = useChat(client, browserMemory, "web");
  const [text, setText] = useState("");
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
        <label>
          Brain{" "}
          <select
            aria-label="Brain"
            value={state.brain ?? ""}
            disabled={busy}
            onChange={(e) => void chooseBrain(e.target.value || null)}
          >
            <option value="">Auto (routing)</option>
            {state.brains.map((b) => (
              <option key={b.name} value={b.name}>
                {b.name} · {b.model}
                {b.priced ? "" : " (cost unknown)"}
              </option>
            ))}
          </select>
        </label>
      </header>

      {state.status === "connecting" && <p role="status">Connecting…</p>}
      {state.status === "offline" && <p role="alert">Gateway unreachable: {state.error}</p>}

      <ol className="items" aria-label="Conversation">
        {state.items.map((item, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
          <li key={i} className={item.role}>
            {item.text}
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
            <strong>{a.tool}</strong> with exactly:
          </p>
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
