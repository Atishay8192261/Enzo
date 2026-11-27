import {
  type AccountsClient,
  browserSign,
  CHATGPT_USAGE_URL,
  type ChatClient,
  type ConversationMemory,
  canSeeImages,
  describeApproval,
  formatCost,
  type ImagesClient,
  STEP_MARK,
  stepLine,
  useChat,
  userText,
} from "@enzo/chat";
import type { ImageSource, UploadedImage } from "@enzo/sdk";
import {
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
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

/** Things a personal agent can do today, to start from. Each fills the composer. */
const SUGGESTIONS = [
  "What's in the news today?",
  "Summarize this article: ",
  "Make a file plan.md with my to-do list",
  "List my open pull requests in ",
];

function greeting(now = new Date()): string {
  const hour = now.getHours();
  if (hour < 5) return "Up late?";
  if (hour < 12) return "Good morning.";
  if (hour < 18) return "Good afternoon.";
  return "Good evening.";
}

/** An image in the composer: a preview of the file, and the gateway's id once it is kept. */
interface Attached {
  key: number;
  name: string;
  preview: string;
  image: UploadedImage | null;
}

/** Said when the chat's model can't see images, before anything is sent (#53). */
export const CANT_SEE =
  "The current model can't see images. Choose one that can in the model menu.";

export function App({ client }: { client: ChatClient & AccountsClient & ImagesClient }) {
  const { state, send, decide, chooseBrain, refreshBrains, newChat, stop } = useChat(
    client,
    browserMemory,
    "web",
  );
  const [text, setText] = useState("");
  const [attached, setAttached] = useState<Attached[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const nextKey = useRef(0);
  const [accountsOpen, setAccountsOpen] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const busy = state.status !== "ready";
  const nothingConnected = state.brains.length > 0 && !state.brains.some((b) => b.available);
  const empty = state.items.length === 0 && !state.draft && state.approvals.length === 0;

  // Follow the conversation as it grows, like a chat should.
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll when these change
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [state.items.length, state.draft, state.approvals.length, state.working]);

  const uploading = attached.some((a) => a.image === null);
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const value = text.trim();
    const ids = attached.flatMap((a) => (a.image ? [a.image.id] : []));
    if ((!value && ids.length === 0) || busy || uploading) return;
    setText("");
    for (const a of attached) URL.revokeObjectURL(a.preview);
    setAttached([]);
    void send(value, ids);
  };

  /** Each image is kept by the gateway at once, so a problem shows before sending. */
  const attach = (files: File[], source: ImageSource) => {
    setImageError(null);
    for (const file of files.filter((f) => f.type.startsWith("image/"))) {
      const key = nextKey.current++;
      const preview = URL.createObjectURL(file);
      setAttached((all) => [...all, { key, name: file.name, preview, image: null }]);
      client
        .uploadImage(file, { source, name: file.name, type: file.type })
        .then((image) =>
          setAttached((all) => all.map((a) => (a.key === key ? { ...a, image } : a))),
        )
        .catch((err: unknown) => {
          URL.revokeObjectURL(preview);
          setAttached((all) => all.filter((a) => a.key !== key));
          const why =
            err instanceof Error ? err.message.replace(/^.*?HTTP \d+: /, "") : String(err);
          setImageError(`Couldn't attach ${file.name || "the image"}: ${why}`);
        });
    }
  };
  const remove = (key: number) =>
    setAttached((all) => {
      const gone = all.find((a) => a.key === key);
      if (gone) URL.revokeObjectURL(gone.preview);
      return all.filter((a) => a.key !== key);
    });
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = [...e.clipboardData.files].filter((f) => f.type.startsWith("image/"));
    if (files.length === 0) return; // text pastes as text
    e.preventDefault();
    attach(files, "pasted");
  };
  const onDrop = (e: DragEvent<HTMLFormElement>) => {
    const files = [...e.dataTransfer.files];
    if (files.length === 0) return;
    e.preventDefault();
    attach(files, "file");
  };

  // Enter sends; Shift+Enter is a new line (and IME composition is left alone).
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
  };

  const suggest = (s: string) => {
    setText(s);
    composer.current?.focus();
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="mark" aria-hidden="true">
            E
          </span>
          <h1>Enzo</h1>
        </div>
        <div className="actions">
          <ModelPicker
            brains={state.brains}
            value={state.brain}
            disabled={busy}
            onChange={(choice) => void chooseBrain(choice)}
          />
          <button type="button" className="ghost" onClick={() => setAccountsOpen(true)}>
            Accounts
          </button>
          <button type="button" className="ghost" disabled={busy} onClick={() => void newChat()}>
            New chat
          </button>
        </div>
      </header>

      {accountsOpen && (
        <AccountsPanel onClose={() => setAccountsOpen(false)}>
          <Accounts client={client} onChange={() => void refreshBrains()} />
        </AccountsPanel>
      )}

      <main className="conversation">
        {state.status === "connecting" && (
          <p className="center muted" role="status">
            Connecting…
          </p>
        )}
        {state.status === "offline" && (
          <p className="banner error" role="alert">
            Gateway unreachable: {state.error}
          </p>
        )}

        {empty && state.status !== "offline" && state.status !== "connecting" && (
          <section className="welcome" aria-label="Welcome">
            <h2>{greeting()}</h2>
            <p className="muted">I'm Enzo, your personal agent. What can I do for you?</p>
            {nothingConnected ? (
              <div className="connect-card">
                <p>Connect a model account to start. Your ChatGPT plan works, no key needed.</p>
                <button type="button" className="primary" onClick={() => setAccountsOpen(true)}>
                  Connect an account
                </button>
              </div>
            ) : (
              <ul className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <li key={s}>
                    <button type="button" onClick={() => suggest(s)}>
                      {s.trim()}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        <ol className="items" aria-label="Conversation">
          {state.items.map((item, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: the transcript only ever appends
            <li key={i} className={item.role}>
              {item.role === "assistant" && (
                <span className="avatar" aria-hidden="true">
                  E
                </span>
              )}
              <div className="body">
                {item.role === "step" ? (
                  <>
                    <span className={`mark ${item.status ?? "ok"}`} aria-hidden="true">
                      {STEP_MARK[item.status ?? "ok"]}
                    </span>{" "}
                    {stepLine(item)}
                  </>
                ) : item.failure === "not_connected" ? (
                  <>
                    No model account is connected yet.{" "}
                    <button type="button" className="link" onClick={() => setAccountsOpen(true)}>
                      Open Accounts
                    </button>{" "}
                    to connect your ChatGPT plan, Anthropic, OpenAI, xAI or Moonshot.
                  </>
                ) : item.role === "user" ? (
                  userText(item)
                ) : (
                  item.text
                )}
                {item.unsent && <small className="unsent"> Not sent</small>}
                {item.offer === "stop" && (
                  <>
                    {" "}
                    <button type="button" className="link" onClick={() => void stop()}>
                      Stop it
                    </button>
                  </>
                )}
                {item.by && <small className="by">{item.by}</small>}
              </div>
            </li>
          ))}
          {state.draft && (
            <li className="assistant draft">
              <span className="avatar" aria-hidden="true">
                E
              </span>
              <div className="body">{state.draft}</div>
            </li>
          )}
          {state.status === "streaming" && !state.draft && state.working === 0 && (
            <li className="notice working" aria-hidden="true">
              <span className="dots" />
            </li>
          )}
        </ol>

        {state.approvals.map((a) => (
          <section key={a.id} className="approval" aria-label="Approval needed">
            <h2>Approve this action?</h2>
            <p className="summary">{describeApproval(a)}</p>
            <p className="label">Exact details</p>
            <pre>{JSON.stringify(a.arguments, null, 2)}</pre>
            {a.detail && (
              <details open className="approval-detail">
                <summary>What it changes</summary>
                <pre>{a.detail}</pre>
              </details>
            )}
            <div className="buttons">
              <button type="button" className="primary" onClick={() => void decide(a.id, true)}>
                Approve
              </button>
              <button type="button" onClick={() => void decide(a.id, false)}>
                Decline
              </button>
            </div>
          </section>
        ))}

        {state.browser && (
          // While Enzo drives the browser for this chat (ADR 0027): where, and how to stop it.
          <p className="banner browser" role="status">
            {browserSign(state.browser)}
            <button type="button" className="link" onClick={() => void stop()}>
              Stop
            </button>
          </p>
        )}
        {state.error && state.status !== "offline" && (
          <p className="banner error" role="alert">
            {state.error}
          </p>
        )}
        <div ref={end} className="end" />
      </main>

      <footer className="dock">
        {attached.length > 0 && (
          <ul className="attachments" aria-label="Images to send">
            {attached.map((a) => (
              <li key={a.key} className={a.image ? "" : "uploading"}>
                <img src={a.preview} alt={a.name || "image"} />
                <button
                  type="button"
                  className="remove"
                  aria-label={`Remove ${a.name || "image"}`}
                  onClick={() => remove(a.key)}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
        {attached.length > 0 && !canSeeImages(state) && (
          <p className="banner error image-warning" role="alert">
            {CANT_SEE}
          </p>
        )}
        {imageError && (
          <p className="banner error image-warning" role="alert">
            {imageError}
          </p>
        )}
        <form
          className="composer"
          onSubmit={submit}
          onDragOver={(e) => e.preventDefault()}
          onDrop={onDrop}
        >
          <button
            type="button"
            className="add-image ghost"
            aria-label="Add an image"
            title="Add an image"
            disabled={state.status === "offline"}
            onClick={() => picker.current?.click()}
          >
            +
          </button>
          <input
            ref={picker}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
            multiple
            hidden
            aria-label="Image file"
            onChange={(e) => {
              attach([...(e.target.files ?? [])], "file");
              e.target.value = "";
            }}
          />
          <textarea
            ref={composer}
            aria-label="Message"
            rows={1}
            value={text}
            placeholder={busy ? "Enzo is working…" : "Message Enzo"}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            disabled={state.status === "offline"}
          />
          {state.status === "streaming" ? (
            // While Enzo writes, the send button becomes Stop (#46); what arrived stays.
            <button type="button" className="send" aria-label="Stop" onClick={() => void stop()}>
              ■
            </button>
          ) : (
            <button
              type="submit"
              className="send"
              aria-label="Send"
              disabled={busy || uploading || (!text.trim() && attached.length === 0)}
            >
              ↑
            </button>
          )}
        </form>
        <small className="meta">
          {formatCost(state.lastCost, state.lastPlan)}
          {state.lastPlan && (
            <>
              {" · "}
              <a href={CHATGPT_USAGE_URL} target="_blank" rel="noopener noreferrer">
                Manage usage
              </a>
            </>
          )}
        </small>
      </footer>
    </div>
  );
}

/** A side panel over the chat. Escape or the backdrop closes it. */
function AccountsPanel({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="overlay">
      <button type="button" className="backdrop" aria-label="Close accounts" onClick={onClose} />
      <div className="panel" role="dialog" aria-modal="true" aria-label="Accounts">
        <button ref={close} type="button" className="close ghost" onClick={onClose}>
          Close
        </button>
        {children}
      </div>
    </div>
  );
}
