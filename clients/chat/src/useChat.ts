import {
  type AgentEvent,
  type Brain,
  type Conversation,
  GatewayError,
  type GatewayEvent,
  GatewayUnreachableError,
  type Usage,
} from "@enzo/sdk";
import { type Dispatch, useCallback, useEffect, useReducer, useRef } from "react";
import {
  type ChatAction,
  type ChatClient,
  type ChatItem,
  type ChatState,
  eventText,
  initialState,
  NO_USAGE,
  type PendingApproval,
  reduce,
  toItems,
} from "./chat.ts";

/** Where a surface keeps the current conversation id (browser storage, a config file, ...). */
export interface ConversationMemory {
  get(): string | null;
  set(id: string): void;
}

interface Connection {
  /** Null: a fresh chat, created when its first message is sent. */
  conversationId: string | null;
  title: string | null;
  brains: Brain[];
  brain: string | null;
  items: ChatItem[];
  approvals: PendingApproval[];
  running: boolean;
  totals: Usage;
}

/**
 * One connection attempt per client. React runs effects twice in development (StrictMode);
 * without this, both runs would create a conversation and race to remember theirs.
 */
const connections = new WeakMap<ChatClient, Promise<Connection>>();

/** How a surface starts: in its last chat (web), or in a fresh one (the terminal, #57). */
export type Start = "last" | "fresh";

function connect(
  client: ChatClient,
  memory: ConversationMemory,
  start: Start,
): Promise<Connection> {
  let attempt = connections.get(client);
  if (!attempt) {
    attempt = open(client, memory, start);
    connections.set(client, attempt);
    attempt.catch(() => connections.delete(client)); // allow a retry after a failure
  }
  return attempt;
}

async function open(
  client: ChatClient,
  memory: ConversationMemory,
  start: Start,
): Promise<Connection> {
  await client.health();
  const brains = await client.brains();
  if (start === "fresh") return { ...FRESH, brains };
  const saved = memory.get();
  let conversation = saved ? await client.conversation(saved).catch(() => null) : null;
  if (!conversation) {
    conversation = await client.createConversation("main");
    memory.set(conversation.id);
  }
  return { ...(await load(client, conversation)), brains };
}

const FRESH: Omit<Connection, "brains"> = {
  conversationId: null,
  title: null,
  brain: null,
  items: [],
  approvals: [],
  running: false,
  totals: NO_USAGE,
};

/** Everything a chat shows when it opens: its words, steps, open cards and what it used. */
async function load(
  client: ChatClient,
  conversation: Conversation,
): Promise<Omit<Connection, "brains">> {
  const [messages, usage, notices] = await Promise.all([
    client.messages(conversation.id),
    // Without these the chat still opens; it just shows no totals or past reminders yet.
    client.usage(conversation.id).catch(() => null),
    client.conversationScheduled(conversation.id).catch(() => []),
  ]);
  const items = toItems(messages, usage, notices);
  return {
    conversationId: conversation.id,
    title: conversation.title,
    brain: conversation.brain,
    items,
    // An approval left open when the app closed is shown again, so it can be answered (#39).
    approvals: (conversation.pending_approvals ?? []).map(
      ({ id, tool, summary, detail, ...rest }) => ({
        id,
        tool,
        arguments: rest.arguments,
        summary,
        detail,
      }),
    ),
    running: conversation.running ?? false,
    totals: usage?.total ?? NO_USAGE,
  };
}

/**
 * Feed a turn's events to the chat. A message the gateway refused never reached Enzo: it is
 * marked "not sent" (#46). A stream that ends without its last event lost the connection: the
 * chat stops waiting and keeps what arrived.
 */
async function pump(
  start: () => AsyncIterable<AgentEvent>,
  dispatch: Dispatch<ChatAction>,
  { sending }: { sending: boolean },
): Promise<void> {
  let heard = false;
  let finished = false;
  try {
    for await (const event of start()) {
      heard = true;
      if (event.type === "done" || event.type === "error") finished = true;
      dispatch({ type: "event", event, at: Date.now() });
    }
  } catch (e: unknown) {
    if (sending && !heard) {
      const busy = e instanceof GatewayError && e.status === 409;
      dispatch(
        busy
          ? { type: "unsent", reason: "busy" }
          : e instanceof GatewayUnreachableError
            ? { type: "unsent", reason: "unreachable" }
            : { type: "unsent", reason: "failed", message: message(e) },
      );
      return;
    }
    if (e instanceof GatewayUnreachableError) {
      dispatch({ type: "gateway", online: false });
      dispatch({ type: "event", event: { type: "error", message: message(e) } });
      return;
    }
    if (!heard) {
      dispatch({ type: "event", event: { type: "error", message: message(e) } });
      return;
    }
  }
  if (!finished) dispatch({ type: "stream_lost" });
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export interface ChatOptions {
  /** How often to check that the gateway still answers (#44). Tests make it short. */
  healthEveryMs?: number;
  /**
   * "last" reopens the chat this surface used last (the default). "fresh" starts a new chat that
   * is only created when its first message is sent, so nothing is drawn twice and no empty chats
   * pile up (#57). New chats started later are made the same way.
   */
  start?: Start;
}

export const HEALTH_EVERY_MS = 3000;

export function useChat(
  client: ChatClient,
  memory: ConversationMemory,
  device: string,
  { healthEveryMs = HEALTH_EVERY_MS, start = "last" }: ChatOptions = {},
) {
  const [state, dispatch] = useReducer(reduce, initialState);
  // The first open succeeded (a fresh chat has no id yet and is still open).
  const connected = state.status !== "connecting" && state.status !== "offline";

  useEffect(() => {
    let cancelled = false;
    connect(client, memory, start).then(
      (c) => {
        if (!cancelled) dispatch({ type: "connected", ...c });
      },
      (e: unknown) => {
        if (!cancelled) dispatch({ type: "offline", error: message(e) });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, memory, start]);

  // Keep checking the gateway: say when it stops answering, and pick up again when it is
  // back, without the user doing anything (#44). Before the first connection succeeded, a
  // successful check connects.
  const checking = useRef(false);
  useEffect(() => {
    const timer = setInterval(() => {
      if (checking.current) return;
      checking.current = true;
      client
        .health()
        .then(
          async () => {
            if (connected) return dispatch({ type: "gateway", online: true });
            dispatch({ type: "connected", ...(await connect(client, memory, start)) });
          },
          () => dispatch({ type: "gateway", online: false }),
        )
        .catch((e: unknown) => dispatch({ type: "offline", error: message(e) }))
        .finally(() => {
          checking.current = false;
        });
    }, healthEveryMs);
    return () => clearInterval(timer);
  }, [client, memory, connected, healthEveryMs, start]);

  const send = useCallback(
    async (text: string) => {
      if (!connected || !text.trim()) return;
      dispatch({ type: "user_sent", text, at: Date.now() });
      let id = state.conversationId;
      if (!id) {
        // A fresh chat becomes real with its first message, with any model chosen before it.
        try {
          id = (await client.createConversation("main", undefined, state.brain)).id;
        } catch (e: unknown) {
          dispatch(
            e instanceof GatewayUnreachableError
              ? { type: "unsent", reason: "unreachable" }
              : { type: "unsent", reason: "failed", message: message(e) },
          );
          return;
        }
        memory.set(id);
        dispatch({ type: "created", conversationId: id });
      }
      const chat = id;
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      await pump(() => client.send(chat, text, { timezone, device }), dispatch, { sending: true });
    },
    [client, memory, connected, state.conversationId, state.brain, device],
  );

  /** Send the last message that did not go through again (/retry, #44). False: there is none. */
  const resend = useCallback(async (): Promise<boolean> => {
    const last = state.items.findLast((i) => i.role === "user");
    if (!last?.unsent) return false;
    await send(last.text);
    return true;
  }, [state.items, send]);

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      const id = state.conversationId;
      if (!id) return;
      try {
        await client.decide(id, approvalId, approved);
        dispatch({ type: "approval_decided", id: approvalId, approved });
        dispatch({ type: "resuming", at: Date.now() });
        await pump(() => client.resume(id), dispatch, { sending: false });
      } catch (e: unknown) {
        dispatch({ type: "event", event: { type: "error", message: message(e) } });
      }
    },
    [client, state.conversationId],
  );

  const chooseBrain = useCallback(
    async (brain: string | null) => {
      const id = state.conversationId;
      // A fresh chat keeps the choice until it is created with it.
      if (id) await client.chooseBrain(id, brain);
      dispatch({ type: "brain_chosen", brain });
    },
    [client, state.conversationId],
  );

  /** Stop the reply being written (Esc in the terminal, Stop on the web). What arrived stays. */
  const stop = useCallback(async (): Promise<boolean> => {
    const id = state.conversationId;
    if (!id) return false;
    try {
      const stopping = await client.stop(id);
      // The browser closes at once on Stop, also while a step waits for an answer and no
      // reply is streaming to say so (ADR 0027).
      if (stopping) dispatch({ type: "event", event: { type: "browser", site: null } });
      return stopping;
    } catch (e: unknown) {
      dispatch({ type: "event", event: { type: "error", message: message(e) } });
      return false;
    }
  }, [client, state.conversationId]);

  /** Start a new conversation; the old one stays saved on the gateway. */
  const newChat = useCallback(async () => {
    if (start === "fresh") {
      dispatch({ type: "connected", ...FRESH, brains: state.brains });
      return;
    }
    try {
      const conversation = await client.createConversation("main");
      memory.set(conversation.id);
      dispatch({
        type: "connected",
        conversationId: conversation.id,
        brains: state.brains,
        brain: conversation.brain,
        items: [],
      });
    } catch (e: unknown) {
      dispatch({ type: "event", event: { type: "error", message: message(e) } });
    }
  }, [client, memory, state.brains, start]);

  /** Open a past chat (#38): its words, steps, open cards and what it used, as it was left. */
  const openChat = useCallback(
    async (conversationId: string) => {
      try {
        const opened = await load(client, await client.conversation(conversationId));
        memory.set(conversationId);
        dispatch({ type: "connected", ...opened, brains: state.brains });
      } catch (e: unknown) {
        dispatch({ type: "event", event: { type: "error", message: message(e) } });
      }
    },
    [client, memory, state.brains],
  );

  /** The chat's new name, after the gateway took it (/rename). */
  const renamed = useCallback((title: string) => dispatch({ type: "renamed", title }), []);
  /** A reminder or task report arrived while the app is open: shown in the chat on screen. */
  const reminded = useCallback((event: GatewayEvent) => {
    const text = eventText(event);
    if (text) dispatch({ type: "scheduled", text });
  }, []);

  /** Reload the catalog, e.g. after an account was connected and more models became available. */
  const refreshBrains = useCallback(async () => {
    dispatch({ type: "brains_loaded", brains: await client.brains() });
  }, [client]);

  return {
    state,
    send,
    resend,
    decide,
    chooseBrain,
    refreshBrains,
    newChat,
    openChat,
    renamed,
    reminded,
    stop,
  } satisfies {
    renamed: (title: string) => void;
    reminded: (event: GatewayEvent) => void;
    state: ChatState;
    openChat: (conversationId: string) => Promise<void>;
    send: (text: string) => Promise<void>;
    resend: () => Promise<boolean>;
    decide: (id: string, approved: boolean) => Promise<void>;
    chooseBrain: (brain: string | null) => Promise<void>;
    refreshBrains: () => Promise<void>;
    newChat: () => Promise<void>;
    stop: () => Promise<boolean>;
  };
}
