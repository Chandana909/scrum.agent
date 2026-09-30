# 02 — Requirements for context management & shared memory

Derived from the architecture PDF (`multi_agent_software_development_architecture.pdf`, §-numbers
below), aamt's `PRD.md` (PRD §), the phased plan (Plan §), and the gaps in
[01-how-aamt-agents-work.md](01-how-aamt-agents-work.md) (G-numbers). Each requirement lists where it
is implemented in `aamt_context`.

## Functional

| ID | Requirement | Source | Implemented in |
|---|---|---|---|
| R1 | Decisions are durable, carry rationale and alternatives, have a status, and can be superseded without losing history ("what did we decide, and what did we decide before that?") | PDF §4, §11, §15 "changes to previous decisions", §19; PRD §16, §22; G6 | `memory.SharedMemory.remember` (subject-keyed supersession, `valid_to`, `superseded_by`, links), `types.MemoryKind.DECISION`, `store.history` |
| R2 | Questions raised and answers given are recorded so agents stop re-asking | PDF §1 | `MemoryKind.CLARIFICATION`; meeting Q/A pairing (`meetings.MinutesExtractor`); brief section "Clarifications (already answered — don't re-ask)" |
| R3 | Every developer gets the context relevant to its task: requirements, conventions, decisions/contracts in force, upstream work, previous attempts and feedback, lessons, relevant code, messages — within a token budget, not the whole history | Plan §5.2; PRD §28.3; G1 | `engine.ContextEngine.build_brief`, `assembly.ContextAssembler`, `codemap.CodeMap` |
| R4 | Project-wide decisions reach every agent even when they are not textually related to its task (avoid incompatible implicit decisions) | Cognition "actions carry implicit decisions"; PDF §3-4 | "Decisions and contracts in force": relevance-ranked first, then all other active ones by importance |
| R5 | A failed attempt's lessons carry into the retry and into later re-runs of the same task | G2; PRD §32 | `attempts.summarize_attempt`, `engine.record_attempt`, `integrations.aamt.AamtTaskContext.retry_context` |
| R6 | Review comments, merge conflicts and blockers on a task are visible when the task is worked on again | G7; PRD §19, §15 | bridge handlers `CODE_REVIEW_COMPLETED`, `MERGE_CONFLICT`, `BLOCKER_*` → task-scoped records → brief "history" section |
| R7 | The working transcript of an agent run stays within the model window; oversized tool outputs are clipped but recoverable | G3 | `clipper.ToolOutputClipper`, `condensers.ToolResultMasker`, `condensers.SummarizingCondenser`, `session.ContextSession` |
| R8 | Context reduction is non-destructive and auditable ("what exactly did the model see at step 17?") | PRD §31 | `worklog.WorkingLog` (log vs view), condensation/edit events persisted in `ctx_items`, `ContextSession.load` |
| R9 | Agents communicate through structured messages that every intended reader receives exactly once | PRD §16; Plan §2.5; G4 | `channels.ChannelHub` (per-reader cursors), `integrations.aamt.ChannelMailbox` |
| R10 | Meetings are time-boxed, logged with timestamps, and turned into decisions/actions/blockers/dependencies/open questions/changes by the Scrum Master | PDF §13-15 | `meetings.MeetingRoom` (open/say/time box/transcript/close), `MinutesExtractor` |
| R11 | Recursive delegation passes an explicit contract down and a bounded report up; a child's decisions/interfaces are reviewed by its parent (bottom-up verification) | PDF §7-9, §12 | `briefs.TaskBrief`, `derive_child_brief`, `HandoffReport`, `engine.record_handoff` (proposals), scope chains |
| R12 | Canonical memory has a single curated writer; others propose (PDF: SM records decisions; human approves artifacts) | PDF §6, §15, §19; PRD §26 | `memory.DefaultWritePolicy`, `accept`/`reject`, `engine.proposals` |
| R13 | Adversarial review can challenge a decision and the answer is recorded | PDF §11 | `SharedMemory.challenge` / `answer`, `open_challenges` shown in briefs |
| R14 | Retrospective lessons persist, recur-detect and reach the right discipline | PRD §21-22; Plan §8.6; G5 | `lessons.LessonConsolidator` (team scoping, support counts, open action items) |
| R15 | Context attaches to work items so any agent (dynamic allocation) gets the same task context | PDF §10 | task/team scopes; `ReaderContext` |
| R16 | Human-readable decision log for reports and SME reviews | PDF §16; PRD §24 | `AamtContextBridge.decision_log_markdown`, meeting minutes summaries |

## Non-functional

| ID | Requirement | Source | How |
|---|---|---|---|
| N1 | Modular: develop independently of aamt, integrate with minimal changes | user request | separate package; framework-free core; adapters in `integrations/`; aamt patch = 6 files / ~60 changed lines + 1 wiring module |
| N2 | Works without an LLM (tests, outages, key exhaustion) | aamt style (every LLM call has a deterministic fallback) | extractive checkpoint, marker-based minutes, keyword team scoping; `llm` is optional everywhere |
| N3 | Idempotent ingestion (resume, replayed events) | Plan §10.3 | `source_key` uniqueness, `store.claim(event:<id>)` after the handler succeeds (failed events are retried), replay-safe `record_handoff` and message ids |
| N4 | Concurrency-safe for parallel/recursive agents | PDF §7-10; config `max_concurrent_agents` | SQLite WAL, `BEGIN IMMEDIATE` transactions with savepoints, compare-and-swap `version`, `store.mutate` for derived updates, per-reader cursors, append-only logs (tested with threads and processes) |
| N5 | Provider-agnostic (OpenRouter/Qwen/Mistral today) | `llm/provider.py` | `TextLLM` protocol; chars/4 token heuristic by default; optional tiktoken |
| N6 | Cheap to operate; inspectable | MVP | SQLite single file per project (`.aamt/context.db`), FTS5 BM25, optional embeddings |
| N7 | Safe with untrusted content (tool output, web, repo files) | PRD §33 | `Trust` levels; tool-derived shared memory is a proposal; rendered fenced "treat as data" |
| N8 | Observable context decisions | PRD §31 | per-brief manifests (sections, tokens, record ids, dropped), per-run logs, access counters |
