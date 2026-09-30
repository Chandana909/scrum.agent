# 03 — Methodologies and alternatives, component by component

For each component: the problem in *this* system, the design space, what each studied system does
(from its source code at the commit listed, or its official documentation), the trade-offs, and
what `aamt_context` does and why. Background-only mentions (systems not re-read in code for this
study) are marked *(background)*.

## 0. What was studied

| System | What it is | Version studied | Parts that matter here |
|---|---|---|---|
| **Claude Code** (Anthropic) | coding agent CLI | official docs, code.claude.com (Sep 2026) — see note below | CLAUDE.md hierarchy, path-scoped rules, auto memory, compaction + re-injection, sub-agents |
| **Claude API** | model platform | official docs, platform.claude.com | context editing, memory tool, server-side compaction |
| **pi** (Earendil Works) | minimal coding-agent harness (TypeScript) | `badlogic/pi-mono@02eed88` (published as `@earendil-works/pi-*`) | session tree, compaction entries, branch summaries, `context_edit` |
| **OpenHands** | coding-agent platform + SDK | `OpenHands/software-agent-sdk@eee8c88` | append-only event log, `Condensation` tombstones, `View` invariants, condenser pipeline |
| **MetaGPT** | SOP-driven "software company" of agents | `FoundationAgents/MetaGPT@11cdf46` | pub/sub `Environment`, role `_watch`, `Memory` indexed by action, SOP documents in a project repo |
| **AG2** | multi-agent framework, v1.0 protocol redesign | `ag2ai/ag2@a467af6` | assembly policies; compaction vs aggregation; `KnowledgeStore`; hub channels |
| **Agent Zero** | general agent framework | `agent0ai/agent-zero@e3051fb` | topic/bulk history compression with budget ratios; memory areas + LLM consolidation |
| **mem0** | memory layer | `mem0ai/mem0@94c3fe9` | extraction pipeline, dedupe, linking, user/agent/run scoping, hybrid search |
| **Letta** | stateful agents (ex-MemGPT) | `letta-ai/letta-code@d4d1db7` | memory as a git-backed file system; reflection sub-agent; shared memory repositories |
| **Graphiti** (Zep) | temporal knowledge graph | `getzep/graphiti@852ca40` | bi-temporal facts; contradiction → invalidation |
| **LangGraph / LangChain / LangMem** | agent framework | langgraph 1.2.12, langgraph-prebuilt 1.1.0, langchain 1.4.3, `langchain-ai/langmem@9d033b4` | checkpointer, Store, `pre_model_hook`, middleware, running summaries, memory manager |
| **Manus** | agent product | blog "Context Engineering for AI Agents" (Jul 2025) | KV-cache discipline, masking, file system as context, recitation, keeping errors |
| **Anthropic engineering** | blogs | "How we built our multi-agent research system" (Jun 2025); "Effective context engineering for AI agents" (Sep 2025) | orchestrator–worker, artifacts by reference, compaction, note-taking, sub-agent summaries |
| **Cognition** | blog | "Don't Build Multi-Agents" (Jun 2025) | share context; implicit decisions conflict |

Notes on the sources you named:

* **"leaked Claude Code documentation"** — not used. Claude Code's official, public documentation
  describes the memory, compaction and sub-agent mechanisms in enough detail for design purposes, and
  building on leaked proprietary material would be a licensing problem for this project. Everything
  about Claude Code below comes from code.claude.com.
* **"zero"** — read as **Agent Zero** (an agent framework, which fits the list); **mem0** ("mem-zero")
  is covered too, since both are directly relevant.
* **"earendil works / pi agent"** — pi's packages are published under `@earendil-works`; the source
  is `badlogic/pi-mono`.
* **Letta** — `letta-ai/letta` is now a landing page; the V1 Python server is archived and its README
  asks that it not be used for comparisons or copied. Only the *current* implementation
  (`letta-code`) and Letta's public concepts are used here.
* **AG2** — as of v1.0 the classic `autogen` namespace (ConversableAgent, GroupChat, TransformMessages)
  moved to `ag2ai/ag2-classic`; the notes describe the new v1 design and mention classic patterns
  where useful.

---

## 1. What to remember: the memory model

**Problem.** Different knowledge has different lifetimes, authority and consumers: an architecture
decision binds everyone until superseded; a failed attempt matters only to the next attempt at that
task; a standup summary is stale in a day; a retro lesson should grow stronger each time it recurs.
aamt stores decisions, messages, standups and retros as separate tables with no lifecycle, and none
of it is retrieved for prompts (01 §5).

**Design space.**

| Approach | Who does it | Strengths | Weaknesses for us |
|---|---|---|---|
| Keep the whole shared transcript | AG2 classic `GroupChat` (every agent sees every message) *(background)*; ChatDev chat chains *(background)* | nothing is lost; trivial | tokens grow with team size × time; attention dilution ("context rot"); conflicts invisible |
| Cognitive taxonomy: semantic / episodic / procedural | LangMem; CoALA *(background)* | clean theory | too coarse to drive governance (a decision and a trivia fact are both "semantic") |
| LLM-extracted atomic facts | mem0 | good for user/profile facts; scoped by `user_id/agent_id/run_id` | facts lose structure (rationale, alternatives, status); designed for personalisation |
| Small editable in-context blocks + archive | Letta core/archival/recall memory (MemGPT concepts) | agent self-edits what matters most | blocks are free text; shared blocks across agents need coordination rules |
| Markdown files: index + topic files | Claude Code auto memory (`MEMORY.md` index, first 200 lines/25 KB loaded; topic files on demand; typed `user/feedback/project/reference`); Letta-code MemFS (root `MEMORY.md`, core files always loaded, deferred directories, `skills/`); Anthropic memory tool (`/memories`) | human-readable, git-friendly, progressive disclosure | concurrency and conflict handling are left to the file system / git |
| Temporal knowledge graph | Graphiti (entities + edges with `valid_at/invalid_at/expired_at`, episode provenance) | time-aware truth, contradiction handling | heavy (graph DB, LLM calls per ingest), overkill at MVP scale |
| SOP documents as the shared memory | MetaGPT (`docs/prd`, `docs/system_design`, `docs/task`, `docs/code_summary` in a git repo) | exactly the PDF's PRD/technical-approach/design artifacts | documents alone aren't queryable per item; no per-item status |

**Choice: typed records with a lifecycle.** `MemoryRecord` (`types.py`) has one of 16 kinds that map
directly to the PDF/PRD vocabulary — `requirement, clarification, assumption, decision, contract,
convention, lesson, fact, blocker, risk, action_item, handoff, attempt, note, summary, artifact` — and
per-kind policy (`KIND_POLICIES`): default importance, recency half-life (none for decisions and
contracts; 48 h for attempts; 3 days for blockers…), whether near-duplicates merge, whether the kind
is *subject-keyed* (one active record per subject), and whether shared writes need curation.

Why typed records rather than files or a graph:

* the kinds drive **governance** (decisions need curation, notes don't), **retrieval** (brief sections
  per kind), **decay** (half-lives) and **rendering** (a decision shows its rationale; a clarification
  shows Q and A);
* records stay text-first (`title` for indexes, `body` for detail, `data` for structure), so an LLM
  reads them directly and they can be rendered into the PDF's markdown artifacts (PRD, ADR log,
  technical-approach) for humans;
* the one thing taken from the graph approach is **bi-temporality** (`valid_from/valid_to`,
  `superseded_by`) and **provenance** (`source_events`, `source_ref`, `author`, `trust`) — the cheap,
  high-value part of Graphiti without a graph database.

---

## 2. Storage and retrieval

**Problem.** Given a task (title, description, criteria, dependencies, files), find the few records
that matter, fast, without an ops burden, with results that can be explained.

**Design space.**

| Approach | Who | Notes |
|---|---|---|
| Vector DB top-k | MetaGPT `RoleZeroLongTermMemory` (Chroma, `memory_k=200` short-term window, older messages moved to RAG); Agent Zero (FAISS areas main/fragments/solutions); CrewAI *(background)* | good on paraphrase; weak on exact identifiers (`T-a1b2c3`, `src/api.py`, `POST /login`) that dominate engineering text |
| Hybrid BM25 + vectors (+ reranker) | mem0 today (lemmatised BM25 + embeddings, optional reranker, md5 dedupe) | best general quality; needs an embedding provider |
| Graph traversal + BM25 + vectors + rerank | Graphiti | highest recall on relations; heaviest |
| Namespaced KV/doc store with optional vector index | LangGraph `BaseStore` (namespace tuple + key, `search(namespace_prefix, query)`, TTL) | simple; no ranking beyond vector similarity; no lifecycle |
| Files + grep/glob on demand | Claude Code ("just-in-time": lightweight identifiers, `grep/head/tail`) | zero index; relies on the agent to search |
| Scored memory stream (recency × importance × relevance) | Generative Agents *(background)* | the classic scoring recipe |

**Choice: SQLite + FTS5 (BM25, Porter stemming) + structured filters + a transparent score, with
optional embeddings.**

* Same technology as aamt's stores; one file per project (`.aamt/context.db`); WAL; transactional.
* FTS5 index over title/body/tags/entities, column weights 5/1/2/3, kept in sync by triggers that fire
  only on content columns (bumping access counters doesn't re-index). Porter stemming was needed:
  without it "error" didn't match "Errors are returned as JSON…" (caught by a test).
* Structured filters in SQL: scope set, kind, status, subject, entity/tag membership (`json_each`),
  sprint, pinned, `as_of`.
* Score (`SharedMemory.recall`), every component reported per hit so manifests can explain ranking:
  `text` (BM25 normalised among candidates) + `entity` (fraction of the task's entities the record
  names) + `importance` (× support bonus) + `recency` (0.5^(age/half-life of the kind)) + `scope`
  (proximity weight) + `sprint` + optional `vector` (cosine). Weights in `RetrievalWeights`.
* Candidates = BM25 hits ∪ entity matches (∪ most-important records when the query is empty).
* Embeddings are a plug (`Embedder` protocol): vectors are computed lazily per candidate, cached by
  content hash, used as a re-rank component. No embedding dependency is shipped because aamt's
  OpenRouter setup has no embedding endpoint wired; add one when paraphrase recall becomes a problem.

---

## 3. Scoping and visibility

**Problem.** The PDF's hierarchy is recursive (Scrum Master → Backend Agent → API sub-agent → REST
sub-sub-agent…). A sub-agent should see project-wide knowledge, its team's knowledge, its ancestors'
task context and its own — but not its siblings' scratch work or another project's memory.

**Design space.** Flat global memory (AG2 classic group chat); per-role private memory plus a
broadcast environment (MetaGPT: each role's `rc.memory` + `Environment`); id partitions (mem0:
`user_id/agent_id/run_id`, at least one required); namespace tuples (LangGraph Store);
blocks attached to several agents (Letta shared blocks, and in letta-code, attached shared memory
repositories); directory hierarchy with inheritance (Claude Code: managed → user → project → local
CLAUDE.md, concatenated root-to-leaf; nested directories load lazily; path-scoped rules).

**Choice: scope paths + visibility as a union of chains** (`types.Scope`, `types.Visibility`).

```
org
p/P-1                                  project
p/P-1/team/backend                     team (domain agent + its sub-agents)
p/P-1/agent/A-7                        one agent's private notes
p/P-1/meeting/MT-3                     a meeting
p/P-1/task/T-1                         a task
p/P-1/task/T-1/task/T-1a               a sub-task delegated from T-1
```

A reader's visibility is `{org} ∪ chain(task path) ∪ {team path} ∪ {agent path}`, each with a weight
(task 1.0, ancestors −0.1 per level, agent 0.85, team 0.8, meeting 0.7, project 0.6, org 0.4). This is
Claude Code's directory inheritance applied to the task tree: a sub-task automatically sees its parent
task's decisions and handoffs; siblings are invisible to each other; the weights make the nearest
knowledge win ties. Scopes decide *access*; kinds, entities and text decide *relevance*.

---

## 4. The write path: governance, de-duplication, conflicts and time

**Problem.** Many writers (seven agents, sub-agents, events, meetings) produce overlapping and
sometimes contradictory knowledge. The PDF makes the Scrum Master the recorder of decisions and a
human the approver of artifacts; Cognition's failure mode is two agents silently making incompatible
decisions.

**Design space.**

| Approach | Who | Risk |
|---|---|---|
| Free writes | most frameworks | pollution, contradictions, no authority |
| LLM decides ADD / UPDATE / DELETE / NONE against similar memories | mem0 classic prompt (still shipped as `DEFAULT_UPDATE_MEMORY_PROMPT`); LangMem `MemoryManager(enable_inserts, enable_updates, enable_deletes=False)` | silent overwrites; hallucinated ids (mem0 maps real UUIDs to small integers precisely to stop this) |
| Additive only + links, resolve at read time | mem0's current default (single-call ADD-only extraction, `linked_memory_ids`, temporal grounding to the observation date, hash dedupe) | contradictions pile up unless readers reconcile |
| LLM consolidation with similarity thresholds | Agent Zero (`merge / replace / update / keep_separate / skip`; replace only above 0.75 similarity; "prefer update/replace for mutable facts"; knowledge sources vs conversation memories) | good hygiene, but an LLM can still drop information |
| Temporal invalidation | Graphiti (`resolve_edge`: duplicates vs contradicted facts; contradicted edges get `invalid_at/expired_at`, never deleted) | needs an LLM judgement per write |
| Background reflection agent | Letta-code reflection sub-agent (reviews transcripts; prioritises corrections, preferences, facts, contradictions, procedures; resolves contradictions in favour of the latest evidence); Claude Code auto memory ("skip anything derivable from the code") | asynchronous; quality depends on the reflector |
| Curated single writer | the PDF (SM records decisions; human approves PRD/approach/design) | a bottleneck, but auditable |

**Choice: deterministic governance, LLMs only for extraction** (`memory.SharedMemory.remember`).

1. **Idempotency** by `source_key` (event ids, decision ids, meeting item keys) — replays are no-ops.
2. **Write policy** (`DefaultWritePolicy`, pluggable): curators (`scrum-master`, `human`) write
   `ACTIVE` anywhere; anyone writes `ACTIVE` in task and meeting scopes; only the owner writes its
   private scope; a team's lead (registered, or role == team name) writes `ACTIVE` in its team scope;
   tool-derived content in shared scopes is always a proposal; curated kinds (decision, contract,
   convention, requirement, assumption, artifact) from non-curators in shared scopes become
   **proposals**.
3. **Exact duplicate** (hash of kind+title+body in the same scope) → *reinforce*: `support += 1`,
   union entities/tags/sources, keep max importance, record who else said it (`seen_by`).
4. **Near duplicate** for kinds where restating should strengthen (lessons, facts, risks, blockers,
   notes, clarifications, action items): BM25 candidates + term-Jaccard ≥ 0.8 → merge. Recurring retro
   lessons become one record with growing support.
5. **Supersession.** Explicit (`supersedes=<id>`) or by **subject**: a subject-keyed kind keeps one
   active record per subject along the scope chain (`api.transport`, `db.engine`, `handoff:T-1`,
   `sprint-goal`). A writer with authority retires the old record — `status=superseded`,
   `valid_to=now`, `superseded_by=<new>`, a `supersedes` link, a history row — nothing is overwritten.
6. **Conflict.** A writer *without* authority (e.g. the database team lead contradicting a project-level
   decision) gets a `CONFLICT` result: the new record is stored as a proposal with `conflicts_with`,
   the old one stays active, and the Scrum Master sees it in `engine.proposals(...)`. `accept()`
   activates it (trust upgraded to `curated`) and retires what it conflicted with; `reject()` records
   the reason.
7. **Challenge / answer** (PDF §11 adversarial review): `challenge()` opens a clarification linked to
   the decision (`challenges` link, `open_challenges` on the decision, shown as "(challenged; under
   review)" in briefs); `answer()` closes it.
8. **Compare-and-swap** on every update (`version`), so concurrent agents can't lose each other's
   writes; every change is in `history` with a full snapshot.

Why not let an LLM decide updates: the PRD asks for "controlled autonomy, traceability, and
reproducibility". A wrong LLM "UPDATE" silently rewrites project truth; a proposal waits for a
curator and is visible to everyone as proposed.

---

## 5. Communication between agents

**Problem.** PRD §16: "Agents should be able to communicate through structured messages"; the PRD
example is an API contract change that the frontend must hear about. aamt's `Mailbox` is unused and
consumes broadcasts globally (G4).

**Design space.** Shared transcript (AG2 classic group chat; ChatDev) — everyone reads everything.
Publish/subscribe message pool (MetaGPT: `Environment.publish_message` routes by address; each
role's `_observe` keeps messages whose `cause_by` is in its `watch` set or that name it in
`send_to`; the role's `Memory` is indexed by `cause_by`). Mailboxes with read flags (aamt). Channels
with membership, write-ahead logs and TTLs (AG2 v1 hub: conversation / discussion / consulting /
workflow channel adapters; per-channel WAL; invite handshake). Handoffs with shared context
variables (AG2 swarm / OpenAI Agents SDK *(background)*).

**Choice: append-only channels, per-reader cursors** (`channels.ChannelHub`). Every message gets a
global `seq`; delivery state is a cursor per `(reader, channel)`, never a flag on the message, so a
broadcast is delivered to each reader exactly once. Conventions: `inbox/<agent>`,
`team/<project>/<team>`, `project/<project>`, `meeting/<project>/<meeting>`. Addressing within a
channel by `recipients` (`*`, agent ids or role aliases); MetaGPT-style type filters
(`unread(types=[...])`). Messages are *communication*, not *memory*: when a message carries durable
knowledge the durable part is written as a record (e.g. `record_handoff` writes an interface as a
contract proposal **and** posts an `API_CONTRACT_UPDATE` to the project channel). `ChannelMailbox`
(integrations/aamt.py) is a drop-in for aamt's `Mailbox` API.

---

## 6. Meetings → minutes → memory

**Problem.** PDF §13-15: time-boxed meetings, every utterance timestamped, and afterwards the Scrum
Master extracts what was discussed, decisions, problems, tasks, blockers, dependencies, open questions
and changes to previous decisions into project state — "this prevents meetings from becoming chat that
gets forgotten".

**Design space.** Free chat kept as transcript (forgotten or too long); LLM summary of the transcript
(lossy, unstructured); structured utterance protocols (explicit markers); turn-taking protocols (a
ceremony concern, not memory).

**Choice** (`meetings.py`): a meeting is a channel plus metadata (kind, agenda, participants,
facilitator, `time_box_s`). `say()` refuses after the time box (unless `allow_overtime` for the
facilitator's wrap-up) and after close. `transcript()` renders exactly the PDF §14 format
(`14:01:12 - Frontend Agent:` + quoted text). `participant_view()` gives a speaker the agenda, time
left and as much of the recent transcript as fits a budget. `close()` runs `MinutesExtractor`:

* **deterministic markers** — `DECISION:` (with optional `because …` rationale and `(subject: x)`),
  `CHANGE: <subject-or-id> -> <new decision>`, `QUESTION:` / `ANSWER:` (paired by `reply_to` or to the
  latest open question), `ACTION: … (owner: X)`, `BLOCKER:`, `DEPENDENCY:`, `PROBLEM:`, `RISK:`,
  `ASSUMPTION:`, `NEW TASK:` — deliberately few markers, because words like "Task:" or "Issue:" appear
  in ordinary speech;
* an **optional LLM pass** that extracts items stated in prose; results are merged and de-duplicated;
  on LLM failure the markers alone are used.

Each item becomes a record written by the facilitator (curator): decisions (subject-keyed; `CHANGE`
supersedes the earlier decision by id or subject), answered clarifications, open questions, action
items with owners, blockers, dependencies, problems/risks, assumptions — plus a `summary` record with
the rendered minutes. Prompting agents to use the markers makes the deterministic path sufficient on
small models.

---

## 7. Context assembly: what goes into an agent's prompt

**Problem.** Per task, choose which knowledge to put in the prompt, in what order, within a budget,
so that the agent has what it needs and nothing that dilutes attention ("context rot": recall
degrades as context grows — Anthropic, Sep 2025).

**Design space.**

| Approach | Who | Notes |
|---|---|---|
| Static template | aamt today (`render_task_context` + `_project_summary`) | no memory at all |
| Dump everything | AG2 classic group chat | expensive, noisy |
| RAG top-k into the prompt | most frameworks | relevance-only: misses important but textually unrelated items |
| Layered instruction files, loaded by scope and lazily | Claude Code: managed/user/project/local CLAUDE.md concatenated; nested files and `paths:`-scoped rules load when matching files are read; skills: descriptions always, bodies on demand | excellent for *standing* knowledge |
| Composable assembly policies | AG2 v1 `AssemblyPolicy.apply(prompts, events) -> (prompts, events)`, composed left to right; `AssemblerMiddleware.validate_order` warns if a reduction policy (token budget) runs before an injection policy (working/episodic memory) | the right shape; budget is a separate reducer |
| Progressive disclosure / just-in-time | Claude Code `MEMORY.md` index + topic files; Letta deferred memory; Anthropic memory tool ("always view your memory directory first"); agents holding lightweight identifiers and loading on demand | keeps prompts small; needs tools |
| Cache-aware prompt layout | Manus: stable prefix, append-only, deterministic serialisation; cached tokens cost ~10× less ($0.30 vs $3/MTok on Sonnet) and their input:output ratio is ~100:1 | cost discipline |

**Choice: budgeted sections** (`assembly.py`, `engine.build_brief`). A context is a list of sections,
each with a priority, a cap (`max_share` of the budget), a tier (static / project / task / volatile)
and a layout `order`. The assembler allocates greedily by priority (required first), lets list
sections drop their lowest-ranked items ("+N more"), trims text sections, then lays out by `order`.
Injection happens *before* reduction by construction: every section is gathered, then the budget cuts.

The aamt brief (priority → layout):

| Section | Content | Priority / cap |
|---|---|---|
| task (optional) | objective, criteria, deliverables, constraints, ownership, interfaces, escalation rules, report format | 100 / 35 % (aamt renders the task itself, so the bridge omits it) |
| history | this task's previous attempts (compressed), review/integration feedback, blockers | 95 / 25 % |
| upstream | latest handoff of every dependency task | 90 / 20 % |
| decisions | decisions/contracts/assumptions **relevant first, then every other active one by importance** | 85 / 20 % |
| requirements | pinned requirements & conventions + relevant ones | 80 / 15 % |
| inbox | unread messages (acked once shown) | 75 / 12 % |
| clarifications | answered Q&A and open questions ("don't re-ask") | 70 / 10 % |
| lessons | relevant lessons, with recurrence count | 65 / 10 % |
| code | outlines of the most relevant files | 60 / 20 % |
| risks | open blockers/risks elsewhere | 45 / 8 % |
| index (optional) | one-liners of more relevant records, for `memory_read` | 20 / 6 % |

Two decisions worth calling out:

* **"All decisions in force", not just relevant ones.** Relevance-only retrieval dropped "Use SQLite
  via the stdlib" from a login task's brief (no shared words) — exactly the implicit-decision failure
  Cognition describes. Decisions and contracts are therefore listed relevant-first and then by
  importance until the section budget is used; nothing important is invisible because of vocabulary.
* **Layout for attention.** Task first, then history and upstream (most actionable), decisions,
  requirements, clarifications, lessons, code, risks, inbox. The whole brief is built once per run and
  sits in the first user message, so the prompt prefix (system + brief) is stable for the entire run —
  cache-friendly by construction.

Every assembly writes a **manifest** (sections, tokens, record ids shown, items dropped, hash of the
project-tier text) to `ctx_items` under `brief:<task>` and bumps `access_count` on the shown records.

---

## 8. Working-context compaction (inside one agent run)

**Problem.** G3: the developer's ReAct transcript grows without bound; tool outputs are large; small
models have 32k windows.

**Design space in detail.**

| System | Trigger | What is reduced | How | Invariants / notes |
|---|---|---|---|---|
| **Claude Code** | automatically as the window approaches its limit; `/compact [focus]`; `/autocompact <tokens>` | the conversation | structured summary replaces history | durable context lives outside history and is **re-injected**: project CLAUDE.md, unscoped rules, auto memory, the plan, a fresh git status, up to 5 recently modified files (files > 5k tokens become references), invoked skill bodies (≤5k each, ≤25k total); nested CLAUDE.md and path-scoped rules are summarised away |
| **Claude API** | `clear_tool_uses_20250919`: default trigger 100k input tokens; `keep` 3 tool uses; `clear_at_least`; `exclude_tools`; `clear_tool_inputs` | old tool results (optionally inputs) | replaced by a placeholder, server-side; client keeps full history | `clear_at_least` exists because each clear invalidates the prompt cache; combine with the memory tool so the model saves what matters before clearing; server-side compaction (on demand or at a threshold) summarises whole history |
| **pi** | `contextTokens > contextWindow − reserveTokens` (default 16 384); after tool results, before the next turn; overflow → compact and retry once | everything before a cut point that keeps `keepRecentTokens` (default 20 000) | structured summary (Goal / Constraints / Progress / Key Decisions / Next Steps / Critical Context + read/modified files); **iterative update** of the previous summary; split-turn handling | never cut at a tool result; `CompactionEntry` appended (non-destructive; `firstKeptEntryId`); `context_edit` entries omit/replace one entry; branch summaries when switching branches |
| **OpenHands SDK** | tokens over limit (hard), event count over `max_size` (soft), explicit request (hard) | the first half of the view (after `keep_first`) | LLM summary of forgotten events, summaries of summaries | append-only log + `Condensation` tombstones (`forgotten_event_ids`, `summary`, `summary_offset`); `View` enforces tool-call matching and batch atomicity via manipulation indices; soft → skip if impossible, hard → full reset with shrinking inputs; `default_condenser` = max 80 events, keep first 4 |
| **AG2 v1** | `CompactTrigger(max_events, max_tokens, chars_per_token=4)` | oldest events | `TailWindowCompact` (drop) or `SummarizeCompact` (summary event) | "Compaction removes. Aggregation creates." Dropped events are persisted to the `KnowledgeStore`; aggregation (working memory, conversation summaries) is a separate, milestone-triggered job |
| **Agent Zero** | over the history budget; compress to 80 % | large messages first, then the middle of the current topic (to 65 %), old topics to request+response, then merge topics into bulks, bulks into bulks | LLM summaries per topic/bulk | budget split 50 % current topic / 30 % history topics / 20 % bulks |
| **LangChain v1** | `SummarizationMiddleware(trigger, keep)`; `ContextEditingMiddleware(ClearToolUsesEdit(trigger=100k, keep=3, …))` | history; old tool results | summary; `"[cleared]"` placeholder | summarisation **rewrites agent state** (`RemoveMessage(REMOVE_ALL_MESSAGES)`); context editing is **view-only** (`request.override`) but not restorable |
| **LangMem** | `max_tokens_before_summary` | messages since the last summary | `RunningSummary` (summarised ids + last id kept in state) | returns `llm_input_messages` — view-only |
| **Manus** | — | — | **restorable compression**: drop content but keep the path/URL; the file system is unlimited context | keep failed actions and errors in context; recite goals (`todo.md`) to keep them in recent attention |
| SWE-agent *(background)* | always | observations older than the last N | collapsed to one line | cheapest effective policy for coding agents |

**Choice: a pipeline over a non-destructive log** (`worklog.py`, `clipper.py`, `condensers.py`,
`session.py`):

1. **Clip at the source** (`ToolOutputClipper`): any tool output over `max_tool_result_tokens`
   (default 2 000) keeps head + tail — *tail-heavy* (25/75) for commands and test runs where the
   summary is at the end — salvages error lines (Traceback, `*Error`, `FAILED`, `E   …`) from the clipped
   middle, and stores the full text as a blob; the agent can page it with `read_stored_output`
   (Manus' restorable compression). In the aamt integration this also shrinks what LangGraph keeps in
   state.
2. **Mask old results** (`ToolResultMasker`, no LLM): above `mask_trigger` (50 % of available), replace
   the oldest tool outputs (except the last 3 and excluded tools) with a one-line stub pointing at the
   stored text, and elide large arguments of old tool calls (`write_file` contents — the Claude API's
   `clear_tool_inputs`). It clears *down to* 75 % of the trigger (hysteresis) so the prefix is not
   rewritten every turn — each rewrite costs a cache miss (why the Claude API has `clear_at_least`).
3. **Summarise** (`SummarizingCondenser`): above `soft_limit` (70 %, skip if no safe cut) or
   `hard_limit` (90 % / explicit request / overflow recovery — must happen), forget the turns between
   the protected prefix (system + task brief) and a verbatim tail of `keep_recent_tokens` (30 %), cut
   only at turn boundaries (never between a tool call and its result), and insert a **structured coding
   checkpoint** (Goal / Constraints & Decisions / Progress / Files / Tests & Errors / Next Steps /
   Critical Context). Iterative: an earlier checkpoint inside the forgotten range is passed as
   `<previous-checkpoint>` and folded. If the LLM fails, an **extractive checkpoint** is built from the
   trace (files read/modified, tools used, last test status, failing test ids, recent error lines, the
   agent's last notes) — so a hard condensation never fails.
4. **Log vs view.** Condensations and edits are events (`Condensation`, `ContextEdit`) applied on read;
   the summary is a virtual entry with a deterministic id so a later condensation can forget it. The
   view repairs call/result pairing (stubs for unanswered calls, like AG2's
   `close_unanswered_tool_calls`). Everything is persisted to `ctx_items`; `ContextSession.load`
   replays a run.
5. **Durable knowledge is not the compactor's job.** Decisions, contracts, requirements, lessons and
   handoffs come from shared memory in the brief (the protected prefix), exactly as Claude Code
   re-injects CLAUDE.md and memory after compaction. The checkpoint only has to carry the run's
   transient working state.

Budgets (`BudgetConfig`): `available = window − reserve_output − tool_schemas`; aamt's wiring uses the
model window setting and `llm_max_tokens` as the reply reserve.

---

## 9. Attempt memory (retries and re-runs)

**Problem.** G2: each retry starts blind; a task reopened in a later sprint starts from nothing.

**Design space.** Discard (aamt). Keep the failed transcript in context (Manus, within one run —
"keep the wrong stuff in"). Verbal self-reflection stored as memory (Reflexion *(background)*).
Structured attempt summaries.

**Choice** (`attempts.py`): after a failure the run's log is reduced deterministically to an
`AttemptSummary` — files modified, tools used, last test status line, failing test ids, recent error
lines, the agent's final claim, the verifier/reviewer feedback — stored as a task-scoped `attempt`
record (48 h half-life). Retries receive "Earlier attempts on this task" + the current feedback (once);
later re-runs of the task see them in the brief's history section. Attempt numbers restart on every
re-run, so the idempotency key includes a per-run id (a bug found and fixed while integrating).

---

## 10. Delegation between levels (recursive sub-agents)

**Problem.** PDF §7-9: any agent may spawn sub-agents; results are verified bottom-up (§12).

**Design space.**

| Approach | Who | Trade-off |
|---|---|---|
| Pass the full parent transcript / share full traces | Cognition principle 1; Claude Code *forks* inherit the parent conversation | maximal context, maximal cost; doesn't scale down a hierarchy |
| Pass only a task string | aamt today | cheap; causes incompatible implicit decisions |
| Structured brief down, condensed result up, artifacts by reference | Anthropic research system (objective, output format, tool guidance, task boundaries; sub-agents return summaries; outputs stored externally, references passed back to avoid a "game of telephone"); Claude Code sub-agents (fresh context; only the final message + a small metadata trailer returns; ~1-2k-token summaries per Anthropic's guidance) | needs an explicit contract |
| Carryover summaries between chats | AG2 classic nested/sequential chats *(background)* | ad hoc |

**Choice** (`briefs.py`, `engine.record_handoff`):

* `TaskBrief` — objective, details, acceptance criteria, deliverables, constraints, **ownership**
  (paths/modules the child may change), **interfaces** it must honour, dependencies, escalation rules,
  a report format, a step budget. `derive_child_brief` inherits constraints and interfaces (they bind
  the subtree), requires ownership to be a subset of the parent's, and halves the step budget by
  default.
* The engine wraps the brief in retrieved shared memory; the child's task scope is nested under the
  parent's, so it sees the parent's decisions and handoffs automatically.
* `HandoffReport` — outcome, summary, files, commits, interfaces, decisions, assumptions, open
  questions, follow-ups, risks, evidence; parsed from simple markers (`SUMMARY:`, `FILES:`,
  `INTERFACE:`, `DECISION:`…) in the child's final message.
* `record_handoff` stores the report as the task's (subject-keyed) handoff and turns its content into
  **proposals** for the parent: decisions and assumptions in the team scope, interfaces as contracts
  in the project scope (plus an `API_CONTRACT_UPDATE` message), open questions, follow-ups (action
  items for triage), risks. That is bottom-up verification: the parent accepts or rejects.

This reconciles Cognition's critique with a hierarchy: what must be shared are the *decisions*
(the carriers of implicit assumptions), not full traces — and decisions reach every agent through the
"decisions in force" section.

---

## 11. Learning across sprints

**Problem.** PRD §21-22: retrospective decisions should become persistent knowledge and recurring
problems should be noticed. G5.

**Design space.** Keyword rules (aamt). Lessons/solutions in vector memory recalled by similarity
(Agent Zero "solutions" area; ChatDev experiential co-learning *(background)*; CrewAI long-term memory
with quality scores *(background)*). Reflection agents that maintain memory and skills (Letta-code
reflection: prefer patterns supported across sessions; skills only for reusable multi-step workflows).
Prompt optimisation from feedback (LangMem).

**Choice** (`lessons.py`): retro items become `lesson` records (negative/change: importance 0.7,
positive: 0.45) scoped to a team when the text clearly concerns one discipline (keyword match; ties →
project scope); action items become open `action_item` records until resolved. Near-duplicate merging
turns recurrence into `support` counts, which boost ranking and show as "(seen 3x)" in briefs. An
optional LLM rewrites notes into reusable imperative lessons. Natural next step: promote high-support,
team-scoped lessons into the PDF's `skills/<domain>/<domain>-agent.md` files (procedural memory).

---

## 12. Code context

**Problem.** Plan §5.2 lists "Relevant Files"; today every attempt rediscovers the repository with
`repo_tree` / `read_file`.

**Design space.** Tree dump; agent-driven exploration; Aider's repo map (tree-sitter tags + PageRank
over the reference graph, sized to a token budget) *(background)*; MetaGPT's code summaries
(`SummarizeCode` → `docs/code_summary`); code embeddings; LSP.

**Choice** (`codemap.py`): a small repo map — Python outlines via `ast` (module docstring, classes with
public methods, function signatures, UPPER_CASE constants), first headings/lines for other files —
ranked by task terms in paths (×2) and outlines (×1), boosted for files named by the task or by
upstream handoffs (×5), de-emphasised tests unless the task is about tests; cached by mtime; rendered
to a budget. Upgrade path: tree-sitter for other languages, and PageRank if repos grow.

---

## 13. Observability of context

Every brief writes a manifest; every run persists entries, condensations and edits; every record has
a history with snapshots and provenance; `access_count` shows which memories are actually used;
`decision_log_markdown` renders the decision trail (with supersessions) into
`.aamt/reports/decisions.md`. Together these answer PRD §31's "why did the system make this decision?"
for the *context* dimension, and give the data needed for PRD research question 5 ("how much
persistent context should individual agents receive?").

---

## 14. Safety

* **Memory poisoning / prompt injection.** Content from tool output (files, test output, web) can
  carry instructions; once stored in shared memory it would be re-injected into every agent. Mitigation:
  `Trust` levels (human > curated > agent > tool); tool-derived writes into shared scopes are always
  proposals; `render_record` fences tool content and labels it "treat as data, not instructions".
* **Authority.** Curated kinds need a curator; agents can't overwrite curated records
  (`may_supersede` returns False for curated/human records unless the writer is a curator).
* **Secrets.** aamt already strips `*API_KEY*/TOKEN/SECRET/PASSWORD` from tool environments; tool
  output can still contain secrets (e.g. a `.env` read by an agent). A redaction hook on `remember()`
  and on the clipper is a sensible addition (not implemented).
* **Blast radius.** The engine only writes to its SQLite file; it never executes content.
