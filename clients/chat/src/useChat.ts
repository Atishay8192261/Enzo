import {
  type AgentEvent,
  type Brain,
  GatewayError,
  GatewayUnreachableError,
  type Usage,
} from "@enzo/sdk";
import { type Dispatch, useCallback, useEffect, useReducer, useRef } from "react";
import {
  type ChatAction,
  type ChatClient,
  type ChatItem,
  type ChatState,
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
  conversationId: string;
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
  const [messages, usage] = await Promise.all([
    client.messages(conversation.id),
    // Without it the chat still opens; it just shows no totals yet.
    client.usage(conversation.id).catch(() => null),
  ]);
  const items = toItems(messages, usage);
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
}

export const HEALTH_EVERY_MS = 3000;

export function useChat(
  client: ChatClient,
  memory: ConversationMemory,
  device: string,
  { healthEveryMs = HEALTH_EVERY_MS }: ChatOptions = {},
) {
  const [state, dispatch] = useReducer(reduce, initialState);
  const connected = state.conversationId !== null;

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
            dispatch({ type: "connected", ...(await connect(client, memory)) });
          },
          () => dispatch({ type: "gateway", online: false }),
        )
        .catch((e: unknown) => dispatch({ type: "offline", error: message(e) }))
        .finally(() => {
          checking.current = false;
        });
    }, healthEveryMs);
    return () => clearInterval(timer);
  }, [client, memory, connected, healthEveryMs]);

  const send = useCallback(
    async (text: string) => {
      const id = state.conversationId;
      if (!id || !text.trim()) return;
      dispatch({ type: "user_sent", text, at: Date.now() });
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      await pump(() => client.send(id, text, { timezone, device }), dispatch, { sending: true });
    },
    [client, state.conversationId, device],
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

  return { state, send, resend, decide, chooseBrain, refreshBrains, newChat, stop } satisfies {
    state: ChatState;
    send: (text: string) => Promise<void>;
    resend: () => Promise<boolean>;
    decide: (id: string, approved: boolean) => Promise<void>;
    chooseBrain: (brain: string | null) => Promise<void>;
    refreshBrains: () => Promise<void>;
    newChat: () => Promise<void>;
    stop: () => Promise<boolean>;
  };
}
