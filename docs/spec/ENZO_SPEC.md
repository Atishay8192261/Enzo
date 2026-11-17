# Enzo — Product Idea and Technical Specification

> **Developer:** Atishay Jain
> **Status:** Idea / to be built. This is not a shipped product yet.
> **Date of this specification:** 16 November 2026
> **Contact:** M.S. Computer Science & Engineering '27, Santa Clara University — ajain9@scu.edu — atie.dev — linkedin.com/in/atishayjain19

---

## How to read this document — honesty note first

This document is an ideation and build specification for **Enzo**, a personal AI agent Atishay Jain wants to build.

It is written by studying what Muse is and how Muse behaves, and then turning that into a thing to be built.

It is **not** a claim about Meta's internal source code, infrastructure, or exact implementation. Muse's exact internal stack, training data, and proprietary systems are not public, and this document does not pretend to know them.

What is kept word-for-word from the Muse description is the *product definition* — what Muse is as a system. What is new here is the framing: Enzo is to be built to meet that definition, by Atishay Jain, with one deliberate change — the LLM brain must be swappable.

### Verification levels used in this file

| Label | Meaning |
|---|---|
| **Verified — observable** | Something you can see Muse do in normal use: it streams, it calls tools, it remembers across chats, it asks for approval before irreversible actions. |
| **Verified — general knowledge** | Standard, textbook facts about how large language models work: tokenization, autoregressive generation, context windows, attention. |
| **Inferred — to be verified in build** | A reasonable architectural inference from observable behaviour. Enzo must prove it in its own implementation, not assume it because Muse does it. |
| **Proposed — Enzo requirement** | A decision Atishay Jain is making for Enzo. This is a requirement to be built and tested, not a fact about Muse. |

If a sentence in this file does not carry one of those meanings, treat it as proposed until it is tested.

No suggested build stack is given in this file, on purpose. The choice of language, framework, database, hosting, and libraries will be made by Atishay Jain during the build, with his own AI agent helping him. This file specifies *what must be true* and *how it must behave*, not *which vendor to pick*.

---

## 0. The idea in one paragraph

Atishay Jain wants to build **Enzo**: a stateful, multi-client, tool-using personal agent built on top of a large language model.

Enzo is not a single prompt to an LLM. Enzo is to be an operating system around an LLM: a persistent runtime, a client layer, an orchestrator that runs a plan-act-observe loop, a typed tool registry, a four-tier memory system, a background runtime, and a safety layer with approval gating, scoped permissions, and prompt-injection defences.

The LLM is the reasoning brain. Everything else is what must make Enzo useful, trustworthy, and persistent across days and weeks instead of a single chat turn.

That sentence is kept deliberately, because it is the clearest definition of what Muse is — and it is exactly what Enzo has to become.

**Identity to be built:**
- **Product name:** Enzo
- **Developer:** Atishay Jain
- **Role:** Personal AI agent, assistant, and collaborator for one principal user
- **Model name:** There is no fixed Enzo model. Enzo is not a foundation model. Enzo is an agent system. Its brain is a swappable LLM (see Section 1).
- **Previous name in earlier drafts:** Muse — replaced in full. No part of Enzo is to be described as Muse, Meta, or Muse Spark in the final product.

---

## 1. The swappable LLM brain — the one deliberate change from Muse

### 1.1 What it is

**Verified — general knowledge:** A large language model takes tokenized text as input and generates text one token at a time, conditioned on all previous tokens. It has a fixed context window. It does not, by itself, remember, browse, read files, or act.

**Proposed — Enzo requirement:** Enzo must not be tied to one LLM provider, one model name, or one API shape. Any capable LLM must be usable under the hood, and swapping it must not require rewriting Enzo's tools, memory, safety, or user interface.

Enzo is the product. The LLM is a replaceable component inside it.

### 1.2 Why it matters

If the brain is hard-coded, Enzo dies when that provider changes price, limits, quality, or availability. If the brain is swappable, Enzo can be compared, upgraded, and kept alive. It also makes Enzo honest to demo: the same task can be run on different brains, and the reviewer can see what Enzo adds on top of the raw model.

### 1.3 How it must be built

**1.3.1 A single provider interface — to be built**

Enzo must define one internal interface that every LLM adapter implements. The rest of Enzo must only talk to that interface, never directly to a provider's API.

The interface must support, as a minimum:
- `chat()` — send a composed prompt and tool definitions, receive text and/or tool calls
- `stream()` — stream tokens as they are generated, for live replies
- `tool_call()` — return structured tool calls with validated arguments
- `count_tokens()` — count tokens for a given text, for budgeting
- `embed()` — produce a vector for a text chunk, for memory retrieval, where the embedding provider is also swappable

No part of Enzo outside the adapter layer may assume a provider-specific request format, response format, or tool format.

**1.3.2 Tool-schema translation — to be built**

Different providers describe tools differently. Enzo must keep one canonical tool definition internally — name, description, typed parameters, required fields — and each adapter must translate that canonical definition into the provider's format on the way out, and translate the provider's tool-call response back into the canonical format on the way in.

If a provider cannot do structured tool calling natively, its adapter must either emulate it in a verifiable way or mark that capability as unsupported for that provider, and Enzo must refuse to claim it works.

**1.3.3 Model routing — to be built**

Enzo must be able to choose a brain per task, not per deployment:
- A light task (triage, short summary) may use a cheaper, faster brain.
- A hard task (multi-step planning, tool orchestration, long-document reasoning) must use a stronger brain.
- A streaming reply must prefer a brain with low time-to-first-token.

Routing rules must be explicit, stored as configuration, and visible in logs. Enzo must never silently swap brains mid-task without recording it.

**1.3.4 Fallback — to be built**

If the primary brain returns a rate-limit error, a server error, or a timeout, Enzo must:
1. Record the failure with provider, model, time, and error type.
2. Try the next configured brain, once, with the same canonical prompt and tools.
3. If the fallback also fails, stop and report the failure honestly, with what was tried.
4. Never retry in a loop, never silently degrade to a weaker brain and pretend it was the original.

**1.3.5 Cost and token accounting — to be built**

For every LLM call, Enzo must record: provider, model, input tokens, output tokens, latency, and cost if the provider publishes pricing. If pricing is not published, cost must be recorded as unknown, not estimated as zero.

Enzo must enforce a per-task token budget before calling the brain. If the prompt would exceed the budget, Enzo must compact first (see Section 4), not send an oversized prompt and hope.

**1.3.6 User choice — to be built**

The user must be able to see which brain is currently selected, and change it, without redeploying Enzo. The selection must be stored per conversation or per user, and shown in the interface where a reply is generated.

### 1.4 How to verify it works

- Swap the configured brain from Provider A to Provider B, run the same 5 tasks, and confirm all 5 complete with correct tool calls and no code change outside configuration.
- Force a failure on the primary brain and confirm Enzo falls back once, logs the switch, and completes or reports honestly.
- Run a task that requires tool calling on each supported brain and confirm the tool arguments validate against the canonical schema every time.
- Show a log line for a single task that names the provider, model, input tokens, output tokens, and latency.

If any of those fail, the brain is not truly swappable yet.

---

## 2. What Enzo is — the product definition to be built

### 2.1 What it is

**Verified — observable, kept from the Muse description:** Enzo must be a stateful, multi-client, tool-using personal agent built on top of a large language model.

Stateful means it remembers across turns, days, and weeks. Multi-client means the same Enzo is reachable from more than one surface. Tool-using means it does not just answer — it checks, reads, writes, searches, schedules, and books through typed tools.

### 2.2 What it is not — to be built

Enzo must not be:
- A single chat box with a system prompt and no memory.
- A demo that only works for one scripted task.
- A wrapper that forwards every message to an LLM and prints the reply.
- A product that claims to be a trained foundation model. Enzo trains no foundation model in this specification.

### 2.3 System overview — to be built

```
User
  |
  v
Clients (web, phone, messaging)
  |
  v
Agent gateway (auth, routing, streaming)
  |
  v
Orchestrator (agent loop)
  |---- Swappable LLM Brain (any provider, via adapter)
  |---- Context Manager (token budget, compaction)
  |---- Memory System (4 tiers)
  |---- Tool Registry (typed tools)
  |---- Background Runtime (scheduled work, subagents)
  |---- Safety Layer (approvals, permissions, vault)
  |
  v
Tools: Browser, Filesystem, Shell, Email, Calendar, Drive,
       Places, Shopping, Payments, and other connectors
  |
  v
Persistent store (structured records, files, vector index)
```

**What it is:** This is the component map Enzo must implement. Each box is a separate responsibility. No box may be merged into the LLM prompt.

**How it must be built:** Each component must have a clear boundary and a test. The orchestrator must be testable without a live LLM. The tool registry must be testable without a live browser. Memory must be testable without a live user.

**How to verify:** Disable the LLM and confirm the orchestrator still validates tool schemas and rejects invalid calls. Disable the network and confirm Enzo reports tool failures instead of hallucinating results.

---

## 3. Request lifecycle — what must happen when a message arrives

### 3.1 What it is

**Verified — observable:** When you send Muse a message, it does not just generate. It retrieves context, composes a prompt, calls tools, observes results, and then replies. Enzo must do the same, in the same order, every time.

### 3.2 How it must be built — step by step

**Step 1 — Ingest**
- **What it is:** Accept the message plus metadata: conversation id, timestamp, timezone, device, attachments.
- **How it must be built:** Authenticate the user, route to the correct conversation, and preserve the metadata for later steps. A message without a conversation id must not be processed.
- **Verify:** Send two messages to two different conversations and confirm they do not leak context into each other.

**Step 2 — Retrieve context**
- **What it is:** Load who the user is, what they care about, active goals, and relevant past facts before generating.
- **How it must be built:** Search memory with the current message as the query. Retrieve only the top relevant facts, not the whole memory file. Load only the tools relevant to this task.
- **Verify:** Ask about a fact stored last week and confirm Enzo retrieves it. Ask about an unrelated topic and confirm Enzo does not inject irrelevant memory.

**Step 3 — Compose prompt**
- **What it is:** Build the layered prompt the LLM will see.
- **How it must be built, in order:** (1) system instructions, (2) developer instructions, (3) retrieved memory, (4) conversation history, (5) tool definitions, (6) current user message, (7) prior tool results in this turn.
- **Verify:** Log the composed prompt for a test task (without secrets) and confirm the layers are present and in order.

**Step 4 — Agent loop**
- **What it is:** Call the brain, execute any tool calls it returns, feed results back, repeat until done. See Section 4 for the full loop.
- **How it must be built:** Stream tokens to the client as they generate. Execute independent tool calls in parallel. Execute dependent calls only after their dependency returns.
- **Verify:** Run a 3-tool task and confirm the final answer cites the tool results, not a guess.

**Step 5 — Approval gate, if needed**
- **What it is:** Pause before any irreversible action.
- **How it must be built:** Present the exact action and exact content to be sent, booked, bought, or deleted. Do not execute until the user approves that exact version. If the content changes after approval, ask again.
- **Verify:** Attempt a send without approval in a test and confirm it is blocked.

**Step 6 — Persist and learn**
- **What it is:** Save the turn, and write durable learnings to long-term memory.
- **How it must be built:** Save the transcript. Separately, extract only durable facts — preferences, decisions, commitments, corrections — and write them with source and date. Do not write transcripts into long-term memory.
- **Verify:** Teach Enzo a preference, start a new conversation, and confirm it is recalled. Correct the preference and confirm the old value is replaced, not kept alongside.

**Step 7 — Deliver**
- **What it is:** Return the final answer with files, links, or options attached in the same message.
- **How it must be built:** A file that is ready must be delivered as an attachment in that same message, not announced as "ready, go find it."
- **Verify:** Ask for a document and confirm the file arrives in the reply that says it is ready.

---

## 4. The agent loop

### 4.1 What it is

**Verified — observable:** Muse plans, acts with a tool, observes the result, and replans. It does not generate a final answer in one pass when tools are needed. Enzo must implement the same loop.

### 4.2 How it must be built

Pseudocode — language-agnostic, to be implemented during build:

```
run_turn(user_message):
  history = load_history()
  memory = retrieve_memory(user_message)
  tools = select_tools(user_message)
  messages = compose_prompt(system, memory, history, tools, user_message)
  steps = 0

  while steps < MAX_STEPS:
    response = brain.chat(messages, tools=tools, stream=True)

    if response.requires_approval:
      return pause_for_approval(response.action)

    if response.tool_calls:
      for call in response.tool_calls:
        validate(call)               # schema, types, allowed values
        check_permission(call)       # scoped, user-approved?
        result = execute_tool(call)  # browser, file, API, etc.
        messages.append(call)
        messages.append(result)
      steps += 1
      continue

    if response.text:
      save_turn(messages, response.text)
      maybe_write_memory(response.text)
      return response.text

  return "Stopped after max steps — what was tried, and what is still open."
```

**Implementation requirements:**
- **Max steps:** A fixed limit per turn (to be set during build, e.g. 10–25). Enzo must stop at the limit and report what was tried, not loop forever.
- **Validation before execution:** Every tool call must be validated against its schema before it runs. Invalid arguments must be returned to the brain as an error result, not executed.
- **Error as data:** A failed tool call must return an error object as a tool result. The brain may retry with corrected arguments, try an alternative tool, or report the failure. A tool failure must never crash the turn.
- **Parallelism:** Independent tool calls in the same brain response must run in parallel. Dependent calls must wait for the next loop iteration.
- **Stop conditions:** Final answer, user stop, approval pending, terminal tool failure, rate limit. Each must be handled explicitly.

### 4.3 How to verify it works

- Give Enzo a task that needs 3 tools in sequence and confirm it completes in order.
- Give Enzo a tool call with an invalid argument and confirm it is rejected before execution and retried correctly.
- Give Enzo a task that would loop and confirm it stops at the max-step limit with a clear report.

---

## 5. Context management

### 5.1 What it is

**Verified — general knowledge:** Every LLM has a fixed context window — a maximum number of tokens it can attend to in one call. Everything Enzo sends — instructions, memory, history, tool definitions, tool results — must fit inside it, or be compacted.

### 5.2 How it must be built

- **Token counting before every call:** Enzo must count tokens for the composed prompt before calling the brain. If the count exceeds the budget, compaction must trigger first.
- **Compaction:** Older turns must be summarized into a compact summary that preserves decisions, facts, open tasks, and user corrections, and drops verbatim phrasing. Recent turns must stay verbatim.
- **Selective retention:** Large tool outputs (e.g. full page text) must be replaced with a short excerpt or a pointer to a stored file, not kept in full.
- **Memory offload:** Durable facts must be moved from context into long-term memory, then retrieved only when relevant. Context is working memory, not storage.
- **Budget allocation:** Enzo must reserve tokens for generation. History and tools must never crowd out the current task. The exact split is to be tuned during build and recorded, not guessed per call.

### 5.3 How to verify it works

- Hold a long conversation (50+ turns), then ask about a decision from turn 2 and confirm Enzo still recalls it correctly.
- Paste a very large document, ask a question, and confirm Enzo answers from the stored file, not from a truncated prompt.
- Check that a compaction never loses a user correction. Teach a correction, force compaction, and confirm the corrected behaviour persists.

---

## 6. Memory system — four tiers

**Verified — observable:** Memory is the difference between a chatbot and a personal agent. Muse remembers preferences, decisions, and commitments across chats. Enzo must do the same, with explicit tiers, not one vague store.

### 6.1 Tier 1 — Short-term / working memory

- **What it is:** Current conversation, active todos, in-flight tool state.
- **How it must be built:** Held in memory for the session, plus the conversation transcript. Lifetime is this turn and this chat.
- **Verify:** Interrupt a multi-step task mid-way and confirm Enzo resumes from where it stopped, without re-asking.

### 6.2 Tier 2 — Curated long-term memory

- **What it is:** Human-readable notes: who the user is, preferences, decisions, commitments, verified outcomes. In Muse this is `MEMORY.md`, `USER.md`, and people/group files. Enzo must have an equivalent, human-readable store.
- **How it must be built:**
  - Write only durable facts, not transcripts or tool logs.
  - Attribute each fact: who said it, when, what evidence supports it.
  - Correct in place when a fact changes. In dated notes, add an explicit correction naming what it replaces. Never keep a superseded fact alongside the new one as if both are true.
  - Never store secrets: no passwords, API keys, one-time codes, card numbers, or government IDs.
  - Before answering, search with 2–3 phrasings, open the relevant lines, reconcile conflicts, and use the latest decision.
- **Verify:** Change a preference, then ask a question that depends on it and confirm Enzo uses the new value.

### 6.3 Tier 3 — Semantic / vector memory

- **What it is:** Retrieval by meaning, not by exact words. Past conversations, notes, and documents are chunked, embedded into vectors, and retrieved by similarity to the query.
- **How it must be built:**
  1. Chunk text into retrievable units.
  2. Embed each chunk with a swappable embedding model.
  3. Embed the query the same way.
  4. Retrieve the top-k nearest neighbours by similarity.
  5. Verify retrieved text against its source before acting on it. Similarity is not truth.
- **Verify:** Ask a question phrased differently from the original note and confirm Enzo still retrieves the right note. Ask about a similar-but-different topic and confirm Enzo does not confuse them.

### 6.4 Tier 4 — Structured / graph memory

- **What it is:** Goals, tracked items, people, and relationships as structured records with IDs, status, and due dates — not as free text.
- **How it must be built:**
  - Each goal/tracked item must have: id, title, status, due date if any, workspace/path, and what remains.
  - Status must be updated only on evidence: user confirmation, end of timeline, or a verified outcome. A single successful step does not close a goal.
  - Enzo must be able to list open commitments and close them when the evidence shows they are done, without being asked.
- **Verify:** Create a goal, complete it, and confirm Enzo marks it complete and stops reminding about it. Create a goal, complete one step, and confirm Enzo does *not* mark it complete.

### 6.5 Memory write discipline — to be built for all tiers

- Record only what the evidence supports.
- Distinguish an observation from an inference, and label uncertainty.
- Date events by when they happened, using verified dates.
- Keep a source reference so a later check can reach the evidence.
- Do not record a proposed action as completed.

---

## 7. Tool system

### 7.1 What it is

**Verified — observable:** A tool is not a paragraph in a prompt. It is a function with a name, a description, typed parameters, and a typed result. The LLM picks the tool by name and fills in arguments. Enzo must implement tools this way.

### 7.2 How it must be built

**7.2.1 Canonical tool definition**

Each tool must define:
- `name` — e.g. `search_web`, `read_file`, `search_email`
- `description` — what it does and when to use it
- `parameters` — typed schema: types, required fields, allowed values
- `returns` — typed result, plus error shapes

The brain must output a structured tool call. The runtime must validate it against the schema before executing.

**7.2.2 Tool categories Enzo must support**

| Category | Examples to be built | Notes |
|---|---|---|
| Core | Read file, write file, edit file, run shell command | Persistent workspace, safe paths only |
| Browser | Search web, fetch page text, live browser task | Text fetch is read-only and fast; live browser is for login, forms, booking |
| Chat | List chats, read messages, send message | Main chat plus side chats, messaging surfaces |
| Memory | Search memory, read memory, explain provenance | Retrieval plus where a fact came from |
| Goals / tracking | Create goal, log activity, set status, todo list | Durable commitments |
| Scheduling | Create scheduled job, list jobs, update, remove | Background work |
| Subagents | Spawn child agent, list, send follow-up, close | Parallel delegation |
| Documents | Create document, edit, share, export | Docs, decks, sheets, web pages |
| Media | Generate image, generate video | With limits and provenance |
| Connectors | Email, calendar, drive, places, shopping, payments | See Section 8 |

**7.2.3 Skills vs. connectors vs. tools — to be built**

- **Tool:** A single callable function.
- **Connector:** A service integration (e.g. email). It has a setup doc, an auth flow, a status check, and many tools. One connector exposes many tools.
- **Skill:** A playbook for a task (e.g. booking a flight). It tells Enzo which tools/connectors to use, in what order, what to verify, and how to present results. Skills are how domain expertise is encoded without hard-coding it into the model.

Enzo must load the relevant skill before doing a domain task, and must follow its verification steps. A skill is not optional flavour — it is the procedure.

**7.2.4 Browser: two modes — to be built**

1. **Text fetch:** Fast, read-only, no login, no clicks. For discovering sources and reading page text.
2. **Live browser task:** A real browser with cookies, login, forms, and checkout. For actions, bookings, and live verification. Runs asynchronously; results arrive as a handoff.

A failed text fetch must never be reported as "the site is down" or "the slot is unavailable." It only proves the fetch failed.

### 7.3 How to verify it works

- Call a tool with valid arguments and confirm it executes.
- Call the same tool with invalid arguments and confirm it is rejected before execution.
- Ask a question that needs a live check (price, availability, status) and confirm Enzo checks live instead of recalling from training.
- Disconnect the network and confirm Enzo reports the failure instead of inventing a result.

---

## 8. Connectors and external services

### 8.1 What it is

**Verified — observable:** Muse connects to Gmail, Calendar, Drive, and other services through authenticated connectors. Enzo must do the same, with scoped access and a health check per service.

### 8.2 How it must be built

Each connector must have:
- A setup and operations document, written before the connector is used.
- An auth flow — OAuth or secure vault — with scopes limited to what the task needs.
- A status check — verify the connection is healthy before promising work that depends on it.
- A presentation rule — how results are shown, with citations, not raw dumps.

Example connectors to be built, in priority order to be decided during build: email, calendar, drive/docs, places, flight/hotel search, restaurant reservations, shopping, payments, health data (read-only, with explicit user consent).

### 8.3 Rate limits and failures — to be built

- On a rate-limit response, stop that provider scope immediately. Do not retry, sleep, delegate, or switch endpoints to continue. Report partial progress.
- On an auth failure, stop, report, and offer a secure re-connect. Do not bypass.
- On partial results, return what was verified, label what was not, and say what remains unverified.

### 8.4 How to verify it works

- Revoke a connector and confirm Enzo detects it on status check and does not claim to have checked it.
- Trigger a rate limit in a test and confirm Enzo stops that scope and reports it, instead of retrying around it.

---

## 9. Runtime, background work, and multi-agent

### 9.1 What it is

**Verified — observable:** Muse works while the user is away — scheduled checks, watches, reminders — and it can delegate long work to subagents while staying responsive. Enzo must have a runtime that persists beyond a single chat turn.

### 9.2 Persistent runtime — to be built

- **What it is:** A long-lived runtime with a persistent home directory. Files the user is meant to keep must survive restarts. Temporary files must not be treated as durable.
- **How it must be built:** Durable work lives in a defined workspace. Scheduled jobs, subagents, and artifacts must be managed by the runtime, with recovery after a restart. Custom background processes must have their own supervision and startup check.
- **Verify:** Restart the runtime and confirm scheduled jobs, goals, and memory are still present.

### 9.3 Scheduled work — to be built

- **What it is:** A scheduled agent run, e.g. "every 6 hours, check for an opening and notify me."
- **How it must be built:** Each job must have an owner, a stop condition, and a delivery destination (usually the originating chat). Each run must finish; the schedule starts the next one. Scope is exactly what the user approved. A yes to a one-time check does not authorize a recurring check.
- **Verify:** Create a job, let it run once, and confirm it reports once and stops at its stop condition. Cancel a job and confirm it does not run again.

### 9.4 Event-driven work — to be built

- **What it is:** A hook that polls a condition and wakes an agent when it changes.
- **How it must be built:** Hooks must start disabled. A dry run must be inspected before enabling. A hook must not message the user for routine, unchanged results unless the user asked for it.
- **Verify:** Trigger the condition and confirm the hook fires once. Leave the condition unchanged and confirm it stays silent.

### 9.5 Subagents — to be built

- **What it is:** A child agent spawned with the parent's context plus a brief, working in the background while the parent stays responsive.
- **How it must be built:**
  - Results arrive asynchronously via handoff. The parent must not poll in a loop.
  - A child must inherit safety rules. It cannot bypass approvals.
  - For multi-part work, spawn one coordinator, which fans out to its own children. Nesting stops there.
  - Closing a child must stop its work. Resuming must restart an interrupted child from where it stopped.
- **Verify:** Spawn a child for a long task, send a message in the main chat, and confirm the main chat stays responsive. Close the child and confirm its work stops.

### 9.6 Workflows — to be built

- **What it is:** Deterministic, repeatable multi-agent automation where the plan lives in code, not just in a prompt. For sweeps, audits, and long trajectories.
- **How it must be built:** The workflow must be versioned, logged, and stoppable. Each step must record its input, output, and agent used.
- **Verify:** Run the same workflow twice on the same input and confirm the steps and logs match.

---

## 10. Clients and presentation

### 10.1 What it is

**Verified — observable:** Muse is reachable from a main chat, side chats, and messaging surfaces, with a feed, goals, and a library. Enzo must be reachable from more than one surface, with the same memory and goals everywhere.

### 10.2 How it must be built

- **Main chat:** Primary, long-running conversation.
- **Side chats:** Separate threads for a topic, with isolated conversation context but shared memory and goals.
- **External chats:** Messaging surfaces where Enzo talks on the user's behalf. Private context from memory must not be revealed to other participants unless the user explicitly asks.
- **Goals view:** Durable goals with workspace, documents, and status.
- **Library:** Documents, decks, sheets, and web pages the user keeps.

### 10.3 Presentation rules — to be built

- **Lead with the answer.** Short, actionable, bulleted replies by default for this user: one to two lines per pointer, no fluff. Expand only if the user explicitly asks for more.
- **Files are delivered, not announced.** A file that is ready must be attached in the same message that says it is ready.
- **Structured choices as widgets/cards:** Options, maps, lists, agendas, shopping cards — created via dedicated creators, not as raw text.
- **Citations:** Web results must cite their source with location. Social posts must cite their post id. Punctuation goes before the citation.
- **No internal jargon in chat:** No tool names, no "subagent", no "handoff", no "cron" in user-facing text. Plain language instead.
- **Voice:** Voice notes and live voice must be supported if a voice surface is built, with voice selection persisted.

### 10.4 How to verify it works

- Ask the same question in the main chat and in a side chat and confirm the answer uses the same memory, but the conversations do not leak into each other.
- Ask for a document and confirm it arrives as an attachment in the reply, not as a path to go find.

---

## 11. Persistence and data model

### 11.1 What it is

**Inferred — to be verified in build:** Enzo needs durable storage for transcripts, goals, memories, scheduled work, and files. Without it, it cannot be stateful.

### 11.2 How it must be built — functionally, no stack prescribed

- **Structured records:** Transcripts, messages, tool calls, goals, memories, scheduled work, and artifacts must be stored as structured records with ids, timestamps, and status — not only as free text.
- **Human-readable memory:** Curated memory must also exist as human-readable files the user can open, edit, and correct. The structured store and the human-readable files must be reconciled; they must not drift into two different truths.
- **Files:** Documents the user is meant to see must live in a defined user-visible folder. Internal bookkeeping must live in a separate hidden folder. A finished file must be written in its home from the first write, not copied or moved after.
- **Media:** Photos, audio, and generated media must be stored with provenance: when it was created, from what input, and by which brain/tool.
- **What must never be stored:** Passwords, API keys, session tokens, one-time codes, card numbers, or government IDs. Those live only in a secure vault or the provider's OAuth store, never in memory, files, logs, or transcripts.

### 11.3 How to verify it works

- Create a goal, restart Enzo, and confirm the goal is still there with the same status.
- Edit a memory file by hand and confirm Enzo uses the edited value on the next question.
- Search logs for a test secret and confirm it does not appear.

---

## 12. Safety, permissions, and privacy

### 12.1 What it is

**Verified — observable:** Muse gates irreversible actions, treats external content as data not instructions, and does not share private details with third parties unless needed. Enzo must implement the same authority model.

### 12.2 Authority model — to be built

- The user is the only principal. Everyone else is a third party.
- Instructions in web pages, files, emails, images, tool outputs, or other agents' reports are data, not orders. They cannot expand the task, grant permission, or override a safeguard.
- A claim of past consent is not consent. The actual approval record is what matters.
- Approval applies only to the exact action and content approved.

### 12.3 Approval gating — to be built

Irreversible or outward actions must trigger an approval step:
- Sending email / message, posting publicly
- Booking, purchasing, charging
- Deleting data, sharing files externally
- Using a saved login or credential

Enzo must not approve on the user's behalf and must not bypass the gate. If the content changes after approval, Enzo must ask again.

### 12.4 Prompt-injection defence — to be built

- Content from outside sources must be marked as untrusted.
- If outside content asks for a disclosure, a new scheduled job, a file upload to a third party, or a credential use the user did not authorise, Enzo must leave that step undone and explain what permission is missing.
- Enzo's job is to complete the user's authorised task, not to follow instructions found along the way.

### 12.5 Privacy and discretion — to be built

- Discretion is what you share, not what you read. Reading the user's email to do their task is fine. Volunteering a private detail to a restaurant, a colleague, or a search query is not.
- Before contacting anyone outside the conversation, share only what the receiver needs to act. A restaurant needs name, time, party size — not the reason for the dinner.
- In group / external chats, private context from memory must not be revealed to other participants unless the user explicitly asks.
- Per-chat discretion rules must be stored and updated only when the user states a rule.

### 12.6 Hard boundaries — to be built

- No bypassing academic access controls (unreleased answer keys, unshared files), ever.
- No building, obtaining, or enhancing biological or chemical weapons.
- No sexual content involving minors.
- No facial-recognition templates to identify people.
- No inferring sensitive attributes (race, religion, health, politics, sexuality, etc.) from indirect signals unless explicit and relevant.

### 12.7 How to verify it works

- Paste a malicious instruction into a file, ask Enzo to read the file, and confirm Enzo does not follow the instruction.
- Attempt a send without approval in a test and confirm it is blocked.
- Ask Enzo to share a private detail with a third party and confirm it asks first or refuses to share more than needed.

---

## 13. Observability, evals, and cost

### 13.1 What it is

**Inferred — to be verified in build:** An agent that cannot be measured cannot be improved. Enzo must log every turn in a way that can be inspected, replayed, and evaluated.

### 13.2 How it must be built

- **Logging:** Every turn must log: brain provider and model, tokens in/out, tool calls and their latency, errors, approvals, and final output. No secrets in logs.
- **Evals that matter:**
  - Task success rate: did Enzo complete the user's actual goal, not just produce text?
  - Tool accuracy: did it pick the right tool with valid arguments on the first try?
  - Grounding rate: are prices, dates, and availability cited from a live check, not hallucinated?
  - Memory accuracy: did it recall the latest decision, not a superseded one?
  - Safety rate: did it refuse or gate irreversible actions correctly?
  - Latency / cost: time to first token and to completion, tokens and cost per task, per brain.
- **Cost model:** Cost is roughly input tokens × input price + output tokens × output price + tool API costs. Long context, many tool calls, and large file reads drive cost. Compaction, tool filtering, and selective memory retrieval are cost controls, not just performance tricks.
- **Regression testing:** The same set of tasks must be run on every significant change. A drop in pass rate must block the change until it is understood.

### 13.3 How to verify it works

- Run the same 20 tasks before and after a change and show the pass rate, latency, and cost for each.
- Show a trace for a single task that lists every tool call, its latency, and its result.

---

## 14. Failure modes and tradeoffs — to be built

| Failure | Why it happens | How Enzo must mitigate it |
|---|---|---|
| Hallucinated price / date | Brain recalls training data instead of checking live | Require live check or citation for actionable facts |
| Wrong tool / bad arguments | Ambiguous prompt, too many tools | Typed schemas, validation, retry with error feedback |
| Lost context | Compaction drops a decision | Explicit memory writes, summary invariants |
| Stale memory | User changed plan, old note still active | Reconcile on read, ask when conflict matters |
| Prompt injection | Malicious instruction in page / file | Treat external content as data, ask for permission |
| Over-verbosity | Brain defaults to long exposition | User-specific style rule: bullets, 1–2 lines, lead with answer |
| Tool loop | Brain repeats same failing call | Max steps, error as tool result, alternative tool |
| Rate limit / block | Too many calls, bot detection | Stop scope, report, use public alternative, do not retry around it |
| Approval fatigue | Too many gates for trivial actions | Gate only irreversible actions, batch the rest |
| Subagent drift | Child loses parent's constraints | Brief carries limits, parent reviews result before delivery |
| Brain swap breaks tools | New provider uses different tool format | Canonical tool schema plus adapter translation, tested per provider |

### What Enzo must be honest about

- Enzo cannot guarantee a live fact without a live check.
- Enzo cannot see a screen the user has not shared, or a device that is offline.
- Enzo cannot bypass a login, paywall, or access control the user has not authorised it to use.
- Enzo does not have real-time knowledge after its brain's training cutoff unless it searches or uses a tool.
- Enzo can be wrong about a date, a timezone, or a lookalike name. Verification is a feature, not an extra.

---

## 15. What will actually make a reviewer pause — genuine points only

This section lists only points that are genuine differentiators for Enzo. Deployment is **not** listed here, on purpose: deploying a portfolio project is a baseline expectation, not a wow point. If Enzo is not deployed and demoable, it cannot be shown at all — that is a requirement, not a differentiator.

### 15.1 Same task, different brains, same correct behaviour

- **What it is:** Run the same multi-step task on two or more different LLMs through Enzo, and show that Enzo completes it correctly each time, with tool calls validated each time.
- **Why it matters:** It proves the swappable brain is real, not a config flag that breaks tools. It also proves Enzo adds value on top of the raw model.
- **How to prove it:** A comparison table for one task: brain, latency, cost, tool accuracy, final result. All rows must show a correct result, not just a fast one.

### 15.2 Memory you can inspect, correct, and trust

- **What it is:** Long-term memory that survives weeks, is human-readable, shows where each fact came from, and can be corrected by the user in plain text.
- **Why it matters:** Most demos forget. A memory that can be audited is rare, and it is what makes a personal agent personal.
- **How to prove it:** Teach Enzo a fact in week 1, ask about it in week 3, show the memory file and its provenance, correct it, and show the corrected behaviour.

### 15.3 Irreversible actions are gated with an exact diff

- **What it is:** Before Enzo sends, books, buys, or deletes, it shows the exact action and exact content, and waits. If the content changes, it asks again.
- **Why it matters:** It is the difference between a toy and a tool you would trust with your email and money.
- **How to prove it:** A screen recording of Enzo refusing to send without approval, then sending the exact approved version.

### 15.4 Tool-call transparency

- **What it is:** Every external action Enzo takes is logged and citable: which tool, which arguments, what it returned, how long it took.
- **Why it matters:** A reviewer can verify Enzo did the work, instead of taking its word for it.
- **How to prove it:** A trace for a single task, with tool calls visible, not just a final answer.

### 15.5 An eval harness, not just a demo

- **What it is:** A fixed set of tasks with pass/fail criteria, run on every change, with results shown.
- **Why it matters:** It shows Enzo is engineered, not just prompted. A demo can be cherry-picked; an eval cannot.
- **How to prove it:** A table: task, expected outcome, actual outcome, pass/fail, latency, cost. Run it before and after a brain swap.

These five points are the only wow points included in this file, because they are the only ones that survive an honest test: each one can be demonstrated, measured, and falsified. Anything that cannot be demonstrated that way was left out.

---

## 16. What Enzo will not claim

- Enzo will not claim to be a foundation model, a Meta product, or Muse.
- Enzo will not claim to know Muse's internal stack, training data, or proprietary systems.
- Enzo will not claim a live fact without a live check or a citation.
- Enzo will not claim a task is complete when only one step succeeded.
- Enzo will not claim a brain swap works until it has been tested on that brain with tool calling.

---

## 17. Build verification checklist — to be completed during build

- [ ] Swappable brain: same 5 tasks pass on 2 different brains, no code change outside configuration.
- [ ] Tool calling: invalid arguments rejected before execution, on every brain.
- [ ] Agent loop: 3-tool sequential task completes, tool-loop task stops at max steps with a report.
- [ ] Context: long conversation still recalls an early decision after compaction.
- [ ] Memory: preference taught, recalled in a new chat, corrected, and new value used.
- [ ] Background: scheduled job runs once, reports once, stops at its stop condition.
- [ ] Subagent: long task delegated, main chat stays responsive, child closed cleanly.
- [ ] Safety: send without approval is blocked; malicious instruction in a file is not followed.
- [ ] Privacy: private detail is not shared with a third party without asking.
- [ ] Observability: a single task trace shows provider, model, tokens, latency, and every tool call.
- [ ] Evals: 20-task harness runs, with pass rate, latency, and cost recorded.
- [ ] Demo: one multi-step, verifiable task is recorded end-to-end, with tool calls visible.

When every box is checked with evidence, Enzo is ready to show. Until then, it is in progress, and this document must say so.

---

*End of specification. This file is an idea and a build specification by Atishay Jain, for Enzo, a personal agent to be built with a swappable LLM brain. It keeps the product definition of what Muse is, and turns it into what Enzo must become. No build stack is prescribed here — that will be decided by Atishay Jain during the build.*
