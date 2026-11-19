import { type AgentEvent, type Brain, GatewayError } from "@enzo/sdk";
import { type Dispatch, useCallback, useEffect, useReducer } from "react";
import {
  type ChatAction,
  type ChatClient,
  type ChatItem,
  type ChatState,
  initialState,
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
  conversationId: string;
  brains: Brain[];
  brain: string | null;
  items: ChatItem[];
  approvals: PendingApproval[];
  running: boolean;
}

/**
 * One connection attempt per client. React runs effects twice in development (StrictMode);
 * without this, both runs would create a conversation and race to remember theirs.
 */
const connections = new WeakMap<ChatClient, Promise<Connection>>();

function connect(client: ChatClient, memory: ConversationMemory): Promise<Connection> {
  let attempt = connections.get(client);
  if (!attempt) {
    attempt = open(client, memory);
    connections.set(client, attempt);
    attempt.catch(() => connections.delete(client)); // allow a retry after a failure
  }
  return attempt;
}

async function open(client: ChatClient, memory: ConversationMemory): Promise<Connection> {
  await client.health();
  const brains = await client.brains();
  const saved = memory.get();
  let conversation = saved ? await client.conversation(saved).catch(() => null) : null;
  if (!conversation) {
    conversation = await client.createConversation("main");
    memory.set(conversation.id);
  }
  const items = toItems(await client.messages(conversation.id));
  return {
    conversationId: conversation.id,
    brains,
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
      dispatch({ type: "event", event });
    }
  } catch (e: unknown) {
    if (sending && !heard) {
      const busy = e instanceof GatewayError && e.status === 409;
      dispatch(
        busy
          ? { type: "unsent", reason: "busy" }
          : { type: "unsent", reason: "failed", message: message(e) },
      );
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

export function useChat(client: ChatClient, memory: ConversationMemory, device: string) {
  const [state, dispatch] = useReducer(reduce, initialState);

  useEffect(() => {
    let cancelled = false;
    connect(client, memory).then(
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
  }, [client, memory]);

  const send = useCallback(
    async (text: string) => {
      const id = state.conversationId;
      if (!id || !text.trim()) return;
      dispatch({ type: "user_sent", text });
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      await pump(() => client.send(id, text, { timezone, device }), dispatch, { sending: true });
    },
    [client, state.conversationId, device],
  );

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      const id = state.conversationId;
      if (!id) return;
      try {
        await client.decide(id, approvalId, approved);
        dispatch({ type: "approval_decided", id: approvalId, approved });
        dispatch({ type: "resuming" });
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
      if (!id) return;
      await client.chooseBrain(id, brain);
      dispatch({ type: "brain_chosen", brain });
    },
    [client, state.conversationId],
  );

  /** Stop the reply being written (Esc in the terminal, Stop on the web). What arrived stays. */
  const stop = useCallback(async (): Promise<boolean> => {
    const id = state.conversationId;
    if (!id) return false;
    try {
      return await client.stop(id);
    } catch (e: unknown) {
      dispatch({ type: "event", event: { type: "error", message: message(e) } });
      return false;
    }
  }, [client, state.conversationId]);

  /** Start a new conversation; the old one stays saved on the gateway. */
  const newChat = useCallback(async () => {
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
  }, [client, memory, state.brains]);

  /** Reload the catalog, e.g. after an account was connected and more models became available. */
  const refreshBrains = useCallback(async () => {
    dispatch({ type: "brains_loaded", brains: await client.brains() });
  }, [client]);

  return { state, send, decide, chooseBrain, refreshBrains, newChat, stop } satisfies {
    state: ChatState;
    send: (text: string) => Promise<void>;
    decide: (id: string, approved: boolean) => Promise<void>;
    chooseBrain: (brain: string | null) => Promise<void>;
    refreshBrains: () => Promise<void>;
    newChat: () => Promise<void>;
    stop: () => Promise<boolean>;
  };
}
