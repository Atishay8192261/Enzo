# ADR 0030 — Search by meaning: a local model, hybrid retrieval, checked against the source

- **Status:** Accepted (2026-11-27)
- **Spec:** §1.3.1 `embed()` ("the embedding provider is also swappable"), §3 step 2, §6.3 (tier 3)
  and its two verify checks, §6.5, §11, §12.5, §13
- **Builds on:** ADR 0001/0005 (Postgres + pgvector), 0007 (memory rides in the new user message),
  0016 (bundled Postgres, verified downloads), 0020/0023 (memory, Memory.md, goals), 0024 (stored texts)

## Context

Recall was Postgres full-text search (ADR 0020): a question with no word in common with a note
("Can I have shrimp scampi?" vs "Allergic to shellfish") found nothing, and any shared word
matched (a theatre note answered a plane-seat question). Past chats could not be searched at all.

## Research (read 2026-11-26/27)

| Option | Finding | Source |
|---|---|---|
| Bundled DB | pixeltable-pgserver 0.7.2 ships Postgres 16.15 + **pgvector 0.8.7** (ivfflat, hnsw, halfvec). Dev/CI apt `postgresql-16-pgvector` here: 0.6.0. | `vector.control` in the installed wheel; `dpkg -l` |
| OpenAI | `text-embedding-3-small`, 1536 dims, $0.02/1M tokens; needs an **API key**: Sign in with ChatGPT's plan route covers the Responses API only. | developers.openai.com embeddings guide + pricing; SIWC "Preview limitations" |
| Anthropic | No embedding model; recommends Voyage AI (`voyage-4` family): a new account. | platform.claude.com embeddings page |
| xAI, Moonshot | Docs unreachable from the cloud session: unverified, not offered. | — |
| Local | all-MiniLM-L6-v2 (Apache-2.0, 22M params, 384 dims, 256-token input) as ONNX: 83 MB download, 90 MB on disk; 0.2 s to load, ~3 ms per question, ~70 ms per 800-char chunk on one core; ~110 MB extra memory once loaded. Archive pinned by SHA-256 as in chromadb 1.5.9's source; revision 7dbbc90. | measured here; `chroma-onnx-models.s3.amazonaws.com` |
| Runtime | onnxruntime 1.32.0 (MIT) + tokenizers 0.23.3. onnxruntime has no Intel-Mac wheel since 1.24 and needs macOS 14 on Apple silicon: `>=1.23.2` lets `uv tool install` pick 1.23.2 there (checked with `uv pip compile --python-platform`). fastembed was not used: it pins its own downloads (no hash), pulls Pillow, and caches in a temp folder. | PyPI JSON, uv |

## Decisions

1. **Local first, swappable.** The default embedder runs on the user's computer: no key, no
   account, nothing sent anywhere to be embedded. It is one `Embedder` (`id`, `tuning`, `prepare`,
   `embed(texts, use=query|document)`), the same shape as `BrainProvider.embed`; a provider
   embedder plugs in behind it. None is offered yet: each would send every memory and chat to the
   provider, needs a measured threshold (no keys here), and its calls need ledger rows.
   `ENZO_EMBEDDINGS=off` turns search by meaning off (words only).
2. **The model is fetched once, verified** (like the terminal app, ADR 0016): on the first start,
   in the background, into `~/.local/share/enzo/models/` (0700). The archive's SHA-256 must match,
   only the two named files are taken from it (never the archive's own paths), each is checked
   again, on every load too. `ENZO_EMBEDDING_MODEL_URL` points at a mirror; the pin still decides.
   Until it is ready (or where ONNX Runtime has no build), Enzo searches by words exactly as before.
3. **What is chunked** (`chunks`, migration 0013, one table): current memories (one each; Memory.md
   is the same rows, read back before each search, ADR 0023), goals (title, status, due, steps),
   past chats a turn at a time (the user's words and Enzo's replies, never tool results or Enzo's
   context parts; 800-char pieces, 120 overlapping, at most 12 per turn) and stored long texts
   (at most 200 pieces each, marked untrusted when outside content). Other people's chats
   (`external`) are never indexed.
4. **Embedded on write, backfilled, re-embedded on a model change.** A background indexer wakes on
   every saved turn and memory change (and each minute): it chunks what is new, then embeds what
   has no vector from the current model, memories first, in passes of 256. Memories and goals
   written moments ago are embedded before a search. Each vector keeps its model's id, and only
   vectors of the current model are compared.
5. **Hybrid retrieval.** Exact cosine nearest neighbours (no ANN index: perfect recall with any
   filter, on pgvector 0.6 and 0.8) plus full-text search where a piece must hold at least half
   of the question's words, fused by reciprocal rank (k = 60).
6. **Only what still holds.** Corrected memories, chats in the trash and external chats are left
   out in SQL; forgotten memories and deleted chats are gone with their rows (cascade).
7. **Verified against the source** (§6.3 step 5): each hit is rebuilt from its source before use
   (memory still current and identical, the chat's messages still give those words, the stored
   text still holds them at that offset, a goal's current words). A hit that fails is dropped.
   The brain gets the source's own words, date and origin.
8. **Measured thresholds** (`tests/semantic/meaning.json`, 26 rephrased questions, 9 confusable
   pairs, 10 unrelated; 6 past turns): memory recall ≥ 0.30, past-chat recall ≥ 0.35, the brain's
   own search ≥ 0.20, and recall keeps only hits within 0.15 of the best (a clearly weaker
   neighbour is a similar-but-different topic). Result with the real model: 24/26 found first,
   23/26 recalled, the neighbour left out in 8/9 pairs (the ninth ranks second, with its own
   words), 9/10 unrelated questions recall nothing.
9. **Recall within ADR 0007 and a budget.** Before each message: up to 6 memories (hybrid) and up
   to 2 turns of *other* chats (by meaning only, each cut to 600 characters) ride in the new user
   message after the history, labelled as records, not instructions; `[context] recall_tokens`
   (1,500) caps both, past chats giving way first.
10. **The brain's search:** `search_past_chats` (past turns, stored texts, goals; this chat
    included), results fenced as data, each with where and when; `search_memory` is hybrid too.
11. **Where text goes.** Embedding: nowhere (local). The model download sends no user data.
    Recalled memories and excerpts go to the chat's own brain provider, as memory already did;
    an excerpt of a chat held with one provider can reach another provider the user connected.
    Logs carry counts and ids only (`semantic.indexed`, `semantic.found`, `semantic.model_*`).

## Consequences

- `/memory` says whether search by meaning is on; `enzo doctor` checks the model;
  `scripts/dev.sh` checks pgvector (FIX: `postgresql-16-pgvector`; on macOS the pgvector install page).
- About 100 MB more installed (onnxruntime, numpy, tokenizers) and 90 MB of model data.
- Exact search scales to tens of thousands of chunks; an HNSW index (with pgvector 0.8's iterative
  scans for filters) is the next step if it grows past that.
- English-only (the model and the full-text configuration). Evals still run memory on words (no
  model download in throwaway runs).
