# 05 — Architecture of `aamt_context`

## 1. Layering

```
integrations/aamt.py        AamtContextBridge, AamtTaskContext, ChannelMailbox   (duck-typed; no aamt import)
integrations/langchain.py   message<->entry, pre_model_hook, ContextMiddleware, wrap_tools, memory_tools,
                            LangChainTextLLM, LangChainChatModel                  (needs langchain-core/langgraph)
───────────────────────────────────────────────────────── core: pydantic + stdlib only ─────────
engine.py        ContextEngine (composition root), ReaderContext, build_brief, record_handoff/attempt
harness.py       AgentLoop (optional minimal agent loop), Tool/ToolSpec/ChatModel protocols
session.py       ContextSession: one agent run's working context
condensers.py    ToolResultMasker, SummarizingCondenser, CondenserPipeline, extractive_checkpoint
clipper.py       ToolOutputClipper (restorable clipping)
worklog.py       Entry, WorkingLog (log vs view), Condensation, ContextEdit, cut_points, repair
attempts.py      AttemptSummary, summarize_attempt
trace.py         deterministic facts from transcripts (files, tools, errors, tests)
assembly.py      Section/TextSection/ListSection, ContextAssembler, AssembledContext (+manifest)
briefs.py        TaskBrief, derive_child_brief, HandoffReport
codemap.py       CodeMap (ast outlines, ranking, budgeted render)
meetings.py      MeetingRoom, MinutesExtractor, MeetingMinutes
lessons.py       LessonConsolidator
channels.py      ChannelHub, Channels (naming), render_message
memory.py        SharedMemory, DefaultWritePolicy, Embedder, render_index_line/render_record
store.py         SqliteMemoryStore (records, FTS, links, history, vectors, channels, cursors, ctx_items, blobs)
types.py         Scope, Visibility, MemoryKind/Status/Trust, KindPolicy, MemoryRecord, ChannelMessage, WriteResult
config.py        BudgetConfig, RetrievalWeights, ContextConfig
llm.py, tokens.py, _util.py
```

Dependencies only point downwards. The core never imports LangChain, LangGraph or aamt.

## 2. Storage schema (`store.py`, one SQLite file per project)

| Table | Purpose | Key columns |
|---|---|---|
| `records` | memory records | `id` (unique), `scope`, `kind`, `status`, `subject`, `title`, `body`, `data` (JSON), `tags`/`entities` (JSON arrays), `importance`, `confidence`, `pinned`, `support`, `author`, `trust`, `source_events`, `source_ref`, `source_key` (unique), `sprint_id`, `content_hash`, `created_at`, `updated_at`, `valid_from`, `valid_to`, `superseded_by`, `version`, `access_count`, `last_accessed_at` |
| `records_fts` | FTS5 (Porter, unicode61) over title/body/tags/entities | external content; triggers on insert/delete and on update *of content columns only* |
| `links` | typed edges between records | `(src, dst, rel)`: `supersedes`, `conflicts`, `proposes_supersede`, `challenges` |
| `history` | every change with a full snapshot | `record_id`, `version`, `op` (create/update/reinforce/supersede/accept/reject/resolve/challenged/answer), `actor`, `ts`, `note`, `snapshot` |
| `vectors` | optional embedding cache | `record_id`, `content_hash`, `vec` |
| `channels` | channel metadata (meetings store their `Meeting` here) | `id`, `kind`, `title`, `participants`, `meta`, `created_at`, `closed_at` |
| `channel_messages` | append-only messages | `seq` (global order), `id`, `channel`, `sender`, `recipients`, `type`, `content`, `data`, `related`, `reply_to`, `ts` |
| `cursors` | per-reader delivery state | `(reader, channel) → seq`, forward-only |
| `ctx_items` | working-context audit log | `(session_id, seq)`, `kind` = entry / condensation / edit / manifest, `payload` |
| `blobs` | full tool outputs behind clipped/masked text | `id`, `session_id`, `meta`, `content` |
| `processed` | idempotency keys for ingestion | `key` (e.g. `event:E-abc123`) |

## 3. Record lifecycle

```
                 remember() by curator / in local scope
   (new) ─────────────────────────────────────────────────────▶ ACTIVE ──resolve()──▶ RESOLVED
     │                                                            │  ▲
     │ remember() by non-curator (curated kind, shared scope)     │  │ accept() (retires conflicts)
     │ or conflict without authority                              │  │
     └──────────────────────────▶ PROPOSED ──────────────────────┘  │
                                     │                               │
                                     └──reject()──▶ REJECTED         │
                                                                     │
   ACTIVE ──(newer record, same subject / explicit supersedes)──▶ SUPERSEDED   (valid_to, superseded_by, link)
   exact/near duplicate of a live record ─▶ no new record; existing one reinforced (support+1)
```

## 4. Main flows

**4.1 Event ingestion (aamt)** — `bus.subscribe(bridge.ingest_event)`:
1. `store.is_claimed("event:<id>")`: an event already processed stops here.
2. Handler by event type (`_on_<type>`). For example, `TASK_COMPLETED` does four things:
   * reads the latest completion-claim note for the task;
   * builds a `HandoffReport` with `HandoffReport.from_text`, adding the commit hash and the files
     from `git diff-tree`;
   * calls `engine.record_handoff` (subject `handoff:<task>`, proposals for decisions and
     interfaces, one transaction, replay-safe);
   * resolves the task's open blockers.

   Handlers are idempotent: every write carries a `source_key` derived from the event id, and
   messages get the id `CM-<event id>`.
3. `store.claim("event:<id>")` runs **only after the handler succeeds**. If a handler fails, for
   example because the database is locked, the error is logged and kept in `bridge.errors`, and the
   event is processed again the next time it is delivered. The producer is never interrupted.

**4.2 Building a brief** — `engine.build_brief(TaskBrief, ReaderContext)`:
1. Visibility from the reader (task chain + team + agent + project + org).
2. Gather sections: task history (attempts, review/integration facts, blockers), upstream handoffs,
   decisions in force (relevant, then all by importance), pinned + relevant requirements/conventions,
   clarifications, lessons, code outline, risks, unread inbox, optional index.
3. `ContextAssembler.assemble` within `brief_tokens`; layout by order.
4. Touch shown records, ack shown messages, persist the manifest.

**4.3 One agent run** (aamt integration):
1. `AamtTaskContext.instrument(tools)` → new `ContextSession` (`<task>#<ctx-uid>#run<n>`), tools wrapped
   (clip + restore), `pre_model_hook`.
2. Before each model call the hook mirrors the graph's messages into the session (by id), runs the
   pipeline (mask → summarise), and returns the view as `llm_input_messages`.
3. After the run, `finish_run(messages)` mirrors the final answer.
4. On failure the task graph calls `record_attempt(attempt, outcome, feedback)`; the retry's
   `extra_context` is `retry_context(feedback)`.

**4.4 Handoff and proposals** — see flow 4.1 step 2 and `record_handoff`.

**4.5 Meeting** — `open` → `say`… (time box) → `close`: minutes extraction → typed records by the
facilitator → minutes summary record → channel closed.

**4.6 Retrospective** — `RETROSPECTIVE_COMPLETED` → read the sprint's retro from aamt's store →
`LessonConsolidator.ingest_retrospective` (team scoping, merge → support, open action items).

## 5. Invariants

1. Nothing is deleted: supersession, rejection, resolution, masking and condensation are all recorded
   state changes or events.
2. Every update is compare-and-swap on `version` and leaves a history snapshot. Derived updates
   (`support + 1`, merged `data`) go through `store.mutate`, which computes them from the current row
   under the write lock, so concurrent writers never lose an update. Multi-record operations are
   atomic: writing a record and retiring what it supersedes, accepting a proposal, recording a
   handoff, and closing a meeting each use one `store.transaction()`.
3. At most one `ACTIVE` record per (kind, subject) along a scope chain, for subject-keyed kinds.
4. Curated or human records are never superseded by a non-curator; the attempt becomes a conflict
   proposal.
5. Tool-derived writes into shared scopes are proposals.
6. A model view never separates a tool call from its result; unanswered calls get a synthetic result;
   orphan results are dropped.
7. The protected prefix of a run (system prompt + first user message) is never condensed.
8. A reader's cursor never moves backwards; a sender never receives its own message.
9. Ingestion of the same host event or `source_key` twice is a no-op.

## 6. Configuration

`BudgetConfig` (per run):

| Knob | Default | Meaning |
|---|---|---|
| `window_tokens` | 32 000 | model context window |
| `reserve_output_tokens` | 4 096 | kept free for the reply (aamt wiring uses `llm_max_tokens`) |
| `tool_schema_tokens` | 1 500 | estimate for tool definitions |
| `mask_ratio` / `soft_ratio` / `hard_ratio` | 0.5 / 0.7 / 0.9 | of `available` |
| `keep_recent_ratio` | 0.3 | verbatim tail kept by summarisation |
| `max_tool_result_tokens` | 2 000 | clip threshold per tool output |
| `keep_last_tool_results` | 3 | never masked |
| `min_maskable_tokens` | 150 | smaller outputs are not worth masking |
| `summary_max_tokens` | 1 200 | checkpoint size cap |

`ContextConfig`: `brief_tokens` 6 000; `candidate_pool` 60; `curators` (`scrum-master`, `human`);
`half_life_hours` overrides per kind; `near_dup_threshold` 0.8; `RetrievalWeights` text 1.0,
entity 0.8, importance 0.5, recency 0.3, scope 0.6, vector 1.0, sprint 0.1.

`KIND_POLICIES` (`types.py`): importance, half-life, near-dup merge, subject-keyed, curated — per kind.

## 7. Extension points

| Protocol / class | Replace to… |
|---|---|
| `TextLLM` | use any model for summaries/minutes/lessons (`LangChainTextLLM`, `FunctionLLM`) |
| `TokenCounter` | exact counts (`TiktokenCounter`) or a provider tokenizer |
| `Embedder` | add semantic re-ranking |
| `WritePolicy` | change who may write what where (e.g. register team leads, stricter human approval) |
| `Condenser` | add policies (e.g. LLM-attention selection, per-tool rules) to a `CondenserPipeline` |
| `Section` | add brief sections (e.g. the PDF's skills, design.md excerpts) via `extra_sections` |
| `ChatModel`, `Tool` | run `AgentLoop` on any model client / tools |

## 8. Performance and limits

* Retrieval is a few indexed SQLite queries per section; a brief with ~10 sections is milliseconds at
  thousands of records. `CodeMap.rank` parses outlines of candidate files once (cached by mtime) —
  noticeable only on very large repos (capped at 3 000 files).
* `ContextSession.view()` recomputes the projection each call (linear in entries) — fine for runs of
  hundreds of steps; memoise if runs get much longer.
* One SQLite connection per engine with an RLock handles threads in one process. For several
  processes on one file:
  * every write transaction starts with `BEGIN IMMEDIATE` (nested blocks become savepoints), so
    read-then-write steps are atomic across processes;
  * `busy_timeout_s` (default 30 s) bounds the wait for another writer;
  * cursors and `claim()` keep delivery and ingestion correct.

  This is tested with 8 threads and with 2 processes reinforcing the same record: no lost updates.
* The default is `synchronous=NORMAL`, SQLite's recommended mode with a write-ahead log (WAL). It
  can't corrupt the file, but a power cut can drop the last few commits. Pass `synchronous="FULL"`
  when every commit must survive a power cut.
* The schema version is kept in `PRAGMA user_version`. A database written by a newer schema raises
  `SchemaVersionError` instead of being misread.
* Measured with 5,000 records on a laptop SSD: `remember` ≈3.4 ms, `recall` ≈8 ms,
  `build_brief` ≈50 ms, and `session.view()` over 600 entries ≈11 ms.
* Token counts are chars/4 estimates by default (conservative); pass `TiktokenCounter` or a
  provider-specific counter for precision.

## 9. Known limitations / next steps

1. Artifacts (PRD.md, technical-approach.md, design.md) are a record kind but not yet rendered/parsed
   automatically; a `render_artifact(kind)` that composes PRD sections from requirements, clarifications,
   decisions and assumptions is the natural next piece (PDF §1-5).
2. Skills (PDF §2): promote high-support team lessons into `skills/<domain>/*.md` and load them as a
   static-tier section.
3. Contradiction detection is subject-based; records without a subject can still disagree. An optional
   Graphiti-style LLM check on decision writes (duplicate vs contradicted) would close the gap.
4. No secret redaction hook yet on `remember()` / the clipper.
5. Brief sections for the Scrum Master's own LLM calls (planning goal, standup, retro) and the reviewer
   (conventions + decisions for "architecture consistency") are easy additions via `build_brief(include=…)`.
6. Parallel/recursive execution is supported by the memory layer but not exercised by aamt yet.
