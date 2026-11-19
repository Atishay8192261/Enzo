import type { AgentEvent, Brain } from "@enzo/sdk";
import { type Dispatch, useCallback, useEffect, useReducer } from "react";
import {
  type ChatAction,
  type ChatClient,
  type ChatItem,
  type ChatState,
  initialState,
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
  return { conversationId: conversation.id, brains, brain: conversation.brain, items };
}

async function pump(
  stream: AsyncIterable<AgentEvent>,
  dispatch: Dispatch<ChatAction>,
): Promise<void> {
  for await (const event of stream) dispatch({ type: "event", event });
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
      await pump(client.send(id, text, { timezone, device }), dispatch).catch((e: unknown) =>
        dispatch({ type: "event", event: { type: "error", message: message(e) } }),
      );
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
        await pump(client.resume(id), dispatch);
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

  return { state, send, decide, chooseBrain, refreshBrains, newChat } satisfies {
    state: ChatState;
    send: (text: string) => Promise<void>;
    decide: (id: string, approved: boolean) => Promise<void>;
    chooseBrain: (brain: string | null) => Promise<void>;
    refreshBrains: () => Promise<void>;
    newChat: () => Promise<void>;
  };
}
