# 08 — Adopt, adapt or keep: building on the open-source projects instead of from scratch

> **Only need the download links?** [09 — Where to get each piece](09-where-to-get-it.md) covers
> just shared memory and context management: links, install commands and classes. This page is the
> full analysis. Its sections on code maps and bank guardrails (§3.11, §3.12) go beyond those two
> parts.

Evaluated on 2026-09-29 against aamt at commit `516a4be`. That commit's loose pins resolve to
langchain 1.4.3, langchain-core 1.6.6 and langgraph 1.2.12. Tests ran on Python 3.11 and 3.12 on
Windows 11.

Evidence markers used below:

* ✅ means installed and run (the scripts are in [`spikes/`](spikes/)).
* ☑ means read in the source at the stated version.
* No mark means a documented behaviour I didn't re-verify.

This document revises part of [04](04-harness-and-langgraph.md) §6. In the inner agent loop, don't
grow our own harness (`AgentLoop`, condensers). Use LangChain v1 `create_agent` with the
DeepAgents/LangChain middleware described below.

---

## 0. The answer on one page

**The combination.**

* **Agent loop and compaction: adopt.** Use LangChain v1 `create_agent` plus middleware:
  * DeepAgents `SummarizationMiddleware` + `SummarizationToolMiddleware`;
  * LangChain `ContextEditingMiddleware`;
  * `count_tokens_approximately(use_usage_metadata_scaling=True)`;
  * later, DeepAgents `FilesystemMiddleware` eviction and `SubAgentMiddleware`.
* **Team memory, briefs, attempts, meetings, lessons, channels: keep `aamt_context`.** Put adopted
  libraries underneath it:
  * sqlite-vec for KNN search (already installed with aamt);
  * fastembed for local embeddings;
  * LangChain structured output for extraction;
  * PII middleware at the edges.
* **Code map: adapt.** Port Aider's repo-map algorithm onto `tree-sitter-language-pack`.
* **Bank-grade guardrails: adopt.** LangChain's `PIIMiddleware`, `HumanInTheLoopMiddleware`,
  `ShellToolMiddleware` + `DockerExecutionPolicy`, `ModelRetryMiddleware` /
  `ModelFallbackMiddleware`, and call limits. All are already installed with aamt. Add
  OpenTelemetry tracing to the bank's collector.

**Why it splits this way.** The projects you named solve two different problems:

1. **Keeping one agent's running transcript inside its window.** This is a solved problem.
   DeepAgents (LangChain's Claude-Code-style harness, 113 releases in 12 months) now ships the same
   design `aamt_context` implemented:
   * a non-destructive view (full state kept, summarised view sent);
   * history offloaded to a file the agent can reopen;
   * an agent-callable compact tool;
   * clearing of old tool results and old tool arguments;
   * overflow recovery.

   It runs in aamt's own environment and adds 12 packages. ✅
2. **Governed memory shared by a team of agents.** Who may decide what, what superseded what, which
   agent sees which scope, provenance, and approval. None of the projects does this:
   * mem0, Graphiti and LangMem have an LLM write and rewrite memory unsupervised;
   * the LangGraph Store and DeepAgents/AG2 memory files are last-write-wins key-value/file stores;
   * Letta's Python server is archived.

   This is the part of `aamt_context` worth owning.

**What that means for the code already written:**

| aamt_context today | Lines | Fate |
|---|---:|---|
| `condensers.py`, `session.py`, `harness.py` (`AgentLoop`), the `WorkingLog` view machinery in `worklog.py` (`Condensation`, `ContextEdit`, `WorkingLog`, `repair`), and in `integrations/langchain.py` `pre_model_hook` / `ContextMiddleware` / `messages_from_entries` / the `AgentLoop` model adapters | ≈900 | **Retire** after migrating to `create_agent` + upstream middleware. Keep until then (the current aamt patch uses them). |
| `clipper.py` | 85 | **Keep** while aamt keeps its own `read_file` tool (see §3.3). Retire when the team adopts DeepAgents' file tools. |
| `codemap.py` | 173 | **Replace** with an Aider-derived tree-sitter repo map. Keep the `ast` outline as a no-dependency fallback. |
| `llm.py` (`complete_json`) | 79 | **Shrink**: structured output does the parsing. Keep only the fallback. |
| `types.py`, `store.py`, `memory.py`, `channels.py`, `meetings.py`, `lessons.py`, `briefs.py`, `assembly.py`, `engine.py`, `attempts.py`, `trace.py`, `tokens.py` (the counter interface), `integrations/aamt.py`, `_util.py`, `config.py` | ≈3,950 | **Keep**: domain logic with no open-source equivalent. Improve it with sqlite-vec, fastembed and structured output. |

**Don't adopt (evidence in §2 and §7):**

* **MetaGPT:** can't co-install with aamt.
* **Letta:** the Python server is archived, and the project forbids new integrations on it.
* **OpenHands SDK as a library:** needs Python ≥ 3.12, adds 158 packages, and its condensers only
  work on its own event model.
* **AG2 v1 and pi:** separate runtimes; adopting either means re-platforming.
* **Agent Zero:** an application, not a library.
* **LangMem:** stale.
* **mem0 and Graphiti** for governed memory: no approval or supersession control, and telemetry is
  on by default.
* **Leaked Claude Code source:** proprietary. It's an IP risk in any product, so don't use it.

---

## 1. How the candidates were judged

1. **Licence** allows use and vendoring in a commercial product: MIT, Apache-2.0 or BSD.
2. **Consumable from Python ≥ 3.11** (aamt's floor), and how:
   * pip dependency;
   * vendored code;
   * subprocess;
   * pattern only.
3. **Co-installs with aamt:**
   * `uv pip compile` of aamt@516a4be + aamt-context + the candidate
     ([`spikes/resolve_with_aamt.sh`](spikes/resolve_with_aamt.sh)) ✅;
   * the full shortlist also co-resolves together on 3.11 (181 packages) and installs cleanly ✅.
4. **Maintained:** PyPI releases in the last 12 months, last push, and whether it's archived.
5. **Solves our problem, not a neighbouring one.** Governed multi-agent project memory is not chat
   personalisation.
6. **Data-egress and telemetry defaults.** This matters for the bank question. Every "ON by
   default" below was read in the source ☑.

## 2. Scorecard: every project you named, plus the ones that turned out to matter

Baseline: aamt + aamt-context resolve to **66 packages** on Python 3.11. "+N" is what the candidate
adds.

| Project | What it really is | Licence | How you'd consume it | Installs with aamt? | Maintenance (12 m) | Telemetry | Verdict |
|---|---|---|---|---|---|---|---|
| **LangChain v1 middleware** (`langchain` 1.4.3) | `create_agent` + ~20 middleware (context editing, summarisation, PII, HITL, shell sandbox, retries, limits) | MIT | already a dependency | ✅ already installed (+0) | 70 releases | LangSmith tracing only if `LANGSMITH_TRACING=true` + key | **ADOPT** |
| **DeepAgents** (`deepagents` 0.7.20) | LangChain's Claude-Code-style harness: file tools + eviction, offloading summarisation, sub-agents, AGENTS.md memory, skills, permissions, rubric grader | MIT | pip, middleware à la carte | ✅ +12 (google-genai, langchain-google-genai, wcmatch…) | 113 releases, 29.9k★ | none of its own | **ADOPT** (middleware; the full harness is optional) |
| **sqlite-vec** 0.1.9 | vector KNN inside SQLite (`vec0`) | MIT / Apache-2.0 | pip | ✅ already installed (langgraph-checkpoint-sqlite depends on it) | 12 releases, pre-1.0 | none | **ADOPT** |
| **fastembed** 0.8.1 | local ONNX embeddings (no torch) | Apache-2.0 | pip | ✅ +17 (onnxruntime, numpy, tokenizers, hf-hub) | 3 releases, 3.2k★ | none; downloads models from Hugging Face on first use | **ADOPT** (optional extra) |
| **Aider repo map** | tree-sitter tags across ~30 languages, def/ref graph, personalised PageRank, token-fitted rendering | Apache-2.0 | vendor ~300 lines of `aider/repomap.py` + its `*-tags.scm` queries | `aider-chat` itself: +76 and downgrades openai/pydantic ✗. The vendored algorithm needs only `tree-sitter-language-pack` (+`grep-ast`) ✅ | aider: 1 release; tree-sitter-language-pack: 62 | none | **ADAPT** |
| **Presidio** 2.2.364 | PII detection (NER + patterns) | MIT | pip, as a `PIIMiddleware` detector | ✅ +31 (spaCy stack) + a spaCy model | 4 releases, 11.1k★ | none | **ADOPT** when agents can see customer data |
| **Langfuse** 4.15.6 / **OpenInference** / **OpenLLMetry** | tracing of every LLM call (inputs, outputs, tokens) | MIT SDK (server MIT + EE dirs) / Apache-2.0 | pip, callback or OTel instrumentor | ✅ Langfuse +13 (OTel SDK) | active | exports only where you point it | **ADOPT one** (OTel to the bank's collector is the neutral choice) |
| **Claude Code** (official docs) | the reference design for compaction, memory files, sub-agents and hooks | proprietary product | patterns (already taken, doc 03) | n/a | n/a | n/a | **PATTERNS ONLY** |
| Claude Code **leaked source** | proprietary Anthropic code | not licensed | — | — | — | — | **DO NOT USE** (IP/licence exposure; everything taken came from the public docs) |
| **Claude Agent SDK** 0.2.161 | Python driver for the Claude Code runtime | MIT package; use governed by Anthropic's Commercial Terms (its README) ☑ | pip + Claude Code CLI | ✅ +19 | 106 releases | Anthropic API | **ONLY IF** the bank approves Claude models (Anthropic, Bedrock, Vertex). Then memory plugs in through its hooks (`PreCompact`, `UserPromptSubmit`, `SessionStart`) ☑ and in-process MCP tools. |
| **pi** (`badlogic/pi-mono`, Earendil) | TypeScript coding agent; the cleanest compaction design | MIT | subprocess via RPC mode (JSONL over stdio) ☑, or port | n/a (Node) | very active, 110k★ | — | **PATTERNS** (already ported: cut points, `reserveTokens`/`keepRecentTokens`, iterative summaries) |
| **Agent Zero** | an application (Docker, web UI), not a package; no `pyproject` ☑ | MIT | copy ideas | n/a | active, 19.3k★ | — | **PATTERNS** (history ratios, consolidation actions) |
| **AG2 v1** (`ag2` 1.1.0, import `ag2`) | new framework: assembly policies, compaction vs aggregation, `KnowledgeStore` (disk/sqlite/redis), streams | Apache-2.0 | pip, but everything takes AG2 `BaseEvent`/`MemoryStream`/`ConversationContext` ☑ | ✅ +2 | 32 releases, 5k★ | own OTel spans | **PATTERNS** (adopting = second agent framework) |
| **MetaGPT** 0.8.2 | multi-agent framework with pub/sub `Environment` and role memories | MIT | — | ✗ **unsatisfiable with aamt** on 3.11 and 3.12; needs Python < 3.12; 66 deps | 0 releases (last 2025-03) | — | **PATTERNS** (watch/`cause_by` routing, already in `channels.py`) |
| **mem0** 2.2.1 | memory layer for personalised assistants: one-call additive LLM extraction, md5 dedup, entity linking, 25+ vector stores ☑ | Apache-2.0 | pip | ✅ +14 | 38 releases, 66.3k★ | **ON by default** (PostHog; `MEM0_TELEMETRY=False`) ☑ | **NO** for governed memory; optional later for agent-private notes (§3.5) |
| **Graphiti** 0.30.2 | bi-temporal knowledge graph built by LLM extraction | Apache-2.0 | pip + **Neo4j / FalkorDB / Neptune** (Kuzu extra deprecated; Kuzu archived 2025-10) ☑ | ✅ +6 (plus a graph DB server) | 49 releases, 31.3k★ | **ON by default** (PostHog; `GRAPHITI_TELEMETRY_ENABLED=false`) ☑ | **NOT NOW** (§3.5) |
| **LangMem** 0.0.30 | memory managers + summarisation node on the LangGraph Store | MIT | pip | ✅ +3 | **1 release** (2025-10); `trustcall` 0 | — | **NO** (superseded by LangChain/DeepAgents middleware) |
| **Letta** | Python server archived; current Letta is TypeScript (`letta-code`) | Apache-2.0 | — | — | — | — | **NO**. The repo's `AGENTS.md` prohibits new integrations, comparisons or copying from the archived server ☑ |
| **OpenHands SDK** 1.49.6 | production coding-agent runtime: condensers, security analyzers, confirmation policies, Docker/remote workspaces | MIT | pip, **Python ≥ 3.12** | ✗ on 3.11. On 3.12 ✅ but +158 packages (litellm, browser-use, boto3, fastapi…) and openai 3.20 → 2.54 | 98 releases | opt-in (Laminar/OTel only if env set) ☑ | **SEPARATE RUNTIME OPTION** for the teammates, not a library for us. Condensers only work on its `View`/`Event` model ☑ |
| **LangGraph Store** (Sqlite/Postgres) | namespaced key-value + vector index, TTL | MIT | already installed | ✅ +0 | active | — | **NOT as the system of record** (last-write-wins, no FTS). Usable as a secondary index or the DeepAgents `StoreBackend` |
| cognee | graph+vector memory platform | Apache-2.0 | pip | ✅ but +132 packages, openai downgrade | active | — | NO (weight; overlaps Graphiti) |
| llm-guard | prompt-injection/PII scanners | MIT | — | — | **archived** | — | NO |
| detect-secrets | secret scanning | Apache-2.0 | — | ✅ | 0 releases since 2024-05 | — | NO. Use regex detectors in `PIIMiddleware` |
| instructor | structured output with retries | MIT | pip | ✅ but pins openai 3.3 | active | — | NOT NEEDED (LangChain structured output is already there) |

## 3. Component by component

For each component: what we have, what upstream offers, the verdict, how to integrate, and what you
give up.

### 3.1 The inner agent loop (aamt `BaseAgent.run`)

* **Today.** `langgraph.prebuilt.create_react_agent(model, tools)`, which is deprecated. The aamt
  patch adds `pre_model_hook` + wrapped tools.
* **Upstream.** `langchain.agents.create_agent(model, tools, system_prompt=…, middleware=[…],
  response_format=…, checkpointer=…)`.
  * It is the documented successor, already installed, and still a LangGraph graph: same
    `invoke(..., config={"recursion_limit": …})`.
  * Middleware get six hooks: `before_agent`, `before_model`, `wrap_model_call`, `after_model`,
    `wrap_tool_call`, `after_agent`. Per-call control is exactly what doc 04 said LangGraph nodes
    lacked; LangChain v1 added it.
* **Verdict: ADOPT.** A two-line change in aamt, shown in §5 step 1.
* **What about DeepAgents' `create_deep_agent`?** It's the same graph with a default stack:
  * file tools on a backend (`ls/read_file/write_file/edit_file/glob/grep/execute`);
  * `task` sub-agents, summarisation, AGENTS.md memory, skills, prompt caching, permissions.

  It's worth it when the teammates want to replace aamt's toolset. Until then, take its middleware à
  la carte with `create_agent`.
* **Give up.** Nothing. `AgentLoop` stays in the repo as a dependency-free reference until the
  migration lands, then goes.

### 3.2 Keeping the running transcript inside the window

This is the single biggest overlap between what I built and what upstream now maintains.

| Capability | `aamt_context` (built) | Upstream | Check |
|---|---|---|---|
| Keep the full log, send a reduced view | `WorkingLog` + `Condensation` tombstones + `view()` | DeepAgents `SummarizationMiddleware` keeps state intact. It stores a private `_summarization_event` (cutoff, summary, file path) and rebuilds the effective messages in `wrap_model_call`. | ✅ 22 messages in state, 11 sent to the model |
| Clear old tool results, with hysteresis | `ToolResultMasker` (trigger, 0.75 hysteresis, keep N) | LangChain `ContextEditingMiddleware(ClearToolUsesEdit(trigger, keep, clear_at_least, exclude_tools, clear_tool_inputs, placeholder))`. This mirrors the Claude API's `clear_tool_uses_20250919`. | ✅ 3 results cleared |
| Structured summary | `SummarizingCondenser` + `CHECKPOINT_FORMAT` | `DEFAULT_SUMMARY_PROMPT` with sections SESSION INTENT / SUMMARY / ARTIFACTS / NEXT STEPS; `summary_prompt=` to override | ✅ |
| Evicted text stays recoverable | persisted to our SQLite (audit only) | offloaded to `/conversation_history/<session>.md` on a backend; the summary names the path | ✅ 14k chars offloaded |
| Agent-requested compaction (Claude Code `/compact`) | `condense(force=True, focus=…)`, API only | `SummarizationToolMiddleware` exposes a `compact_conversation` tool | ☑ |
| Clip huge old tool-call arguments | masker elides args > 800 chars | `truncate_args_settings={"trigger", "keep", "max_length"}` | ☑ |
| Recover when the provider says "too long" | `is_context_overflow` in `AgentLoop` | catches `ContextOverflowError`, summarises and retries | ☑ |
| Never split a tool call from its result | `cut_points` + `repair` | pair-safe cutoff + `PatchToolCallsMiddleware` | ☑ |
| Summary without an LLM (extractive fallback) | `extractive_checkpoint` | none (the summariser is an LLM call) | minor gap |
| Token counts calibrated to the real tokenizer | chars/4 or tiktoken | `count_tokens_approximately(..., use_usage_metadata_scaling=True, tools=…)` scales by the provider's reported `usage_metadata` | ☑ |
| Memory of previous aamt attempts | `AttemptSummary` | none | **keep ours** |
| Team context in the prompt | `ContextEngine.build_brief` | none | **keep ours** |

* **Verdict: ADOPT**, using:
  * DeepAgents `SummarizationMiddleware` + `SummarizationToolMiddleware`;
  * LangChain `ContextEditingMiddleware`;
  * `count_tokens_approximately` with usage scaling.
* **Retire** the ≈900 lines listed in §0 after migration.
* **Settings for aamt's 32k window.** Fractional triggers (`("fraction", 0.8)`) need a model
  profile, which OpenRouter models via `ChatOpenAI` may not have. Use token counts:
  * summarise at 75% of the window, keeping about 25% of the most recent messages;
  * clear tool results from 50%, keeping the last 3;
  * truncate old arguments from 50%.
* **Give up.**
  * The extractive fallback. It matters only if the summary model is down, and then
    `ModelFallbackMiddleware` is the better answer.
  * Our per-edit persistence of views in our DB. The offload file, LangGraph checkpoints and
    tracing cover the audit instead.

### 3.3 Oversized tool output (test logs, big files)

* **Upstream.** DeepAgents `FilesystemMiddleware(backend, tools=[...], tool_token_limit_before_evict=N)`.
  * Output over N tokens goes to `/large_tool_results/<call-id>`.
  * The model gets a head+tail preview and an instruction to page through it with `read_file`
    (offset/limit).
  * ✅ The preview kept the `FAILED …` line from the end of a 900-line test log.
* **The trap I found ✅.** aamt already has a tool called `read_file`, which reads the workspace.
  * `create_agent` silently keeps one of two same-named tools, and it was **aamt's**.
  * So the eviction message would tell the model to `read_file("/large_tool_results/c100")`, and
    aamt's tool can't serve that path.
  * The same applies to the summary's pointer at `/conversation_history/…`.
* **Verdict, two paths:**
  * **A (now, no changes to teammates' tools):** keep `clipper.py`. Its reader tool is named
    `read_stored_output`, so nothing collides. Put the summarisation offload on a
    `FilesystemBackend` rooted **outside** the git workspace, so it is an audit artefact and never
    committed.
  * **B (when the team adopts DeepAgents' file tools):**
    * drop aamt's `list_files/read_file/search_code/write_file` for DeepAgents' `ls/read_file/glob/grep/write_file/edit_file`;
    * run them on `CompositeBackend(default=FilesystemBackend(workspace, virtual_mode=True), routes={"/large_tool_results/": <artifacts>, "/conversation_history/": <artifacts>})`.

    Eviction and history recovery then work natively, and `edit_file` (string replace) is much
    cheaper in tokens than aamt's whole-file `write_file`. Retire `clipper.py`.
* **Either way.** Add a startup assertion that tool names are unique. A silent override is exactly
  the kind of bug that surfaces months later.

### 3.4 Token counting

* **Adopt.** Use `functools.partial(count_tokens_approximately, use_usage_metadata_scaling=True)`
  everywhere a count is needed: middleware triggers, brief budgets, clipping.
* **Why it's better.** It is the chars/4 heuristic multiplied by the provider-reported ratio from
  the most recent AI message. It self-corrects per model, and Qwen/Mistral tokenizers differ from
  tiktoken.
* **Keep** `TokenCounter` as our protocol so the core stays framework-free. The LangChain adapter
  just supplies this function.

### 3.5 Governed shared memory: the part to keep

The requirements come from [02](02-requirements.md): decisions with rationale and supersession,
contracts, clarifications, lessons, handoffs, and who may make them canonical.

| Need | `aamt_context` | mem0 2.2 | Graphiti 0.30 | LangMem | LangGraph Store | AG2 `KnowledgeStore` | DeepAgents memory files |
|---|---|---|---|---|---|---|---|
| Typed records with per-type policy | 16 kinds, `KIND_POLICIES` | free-text facts + metadata | custom entity/edge types | Pydantic schemas | JSON values | files | Markdown |
| Scopes: org / project / team / agent / task, weighted visibility | scope paths + `Visibility` chain | `user_id` / `agent_id` / `run_id` | `group_id` | namespace templates | namespace tuples, prefix search | paths | paths + permissions |
| Who may make memory canonical | write policy, `PROPOSED` → accept/reject, curators | LLM extracts and writes | LLM extracts and writes | LLM | anyone | anyone | agent edits files (the default prompt tells it to) ☑ |
| Supersession with history ("what did we believe on Tuesday?") | subject-keyed, `valid_to`, `superseded_by`, history table | **ADD-only** extraction + md5 dedup ☑ | bi-temporal edges, invalidated **by the LLM** | LLM update/delete | overwrite | overwrite | overwrite |
| Concurrent writers | compare-and-swap versions + retry | depends on the vector DB | graph DB transactions | last write wins | last write wins | last write wins | last write wins |
| Provenance and trust (tool output vs agent vs human) | author, role, trust, source event | actor/role | episode source | — | — | — | — |
| Retrieval | FTS5 BM25 + entity + importance + recency + scope (+ vector re-rank) | vector (+ rerankers) | BM25 + cosine + graph BFS + rerank | vector | vector | none | grep/glob |
| Works with no LLM and no server | yes | no | no (and needs a graph DB) | no | yes | yes | yes |
| Telemetry off by default | yes | **no** ☑ | **no** ☑ | yes | yes | yes | yes |

**Verdict: KEEP.** mem0's extraction prompt is written for user personalisation ("User was
recommended X…") ☑.

For a bank, the columns that decide it are:

* who may make memory canonical;
* supersession;
* provenance.

No project has them. That is the reason `aamt_context` exists.

**Adopt around it:**

1. **Semantic candidates: sqlite-vec + fastembed.** See §3.6.
2. **Production backend.** Implement the same `SqliteMemoryStore` interface on Postgres:
   * `tsvector` for FTS and `pgvector` for KNN (`psycopg` 3 + `pgvector`, both already resolvable
     ✅);
   * `SELECT … FOR UPDATE` / version columns for compare-and-swap.

   Don't use LangGraph's `PostgresStore` as the system of record: it has no compare-and-swap and no
   full-text search.
3. **Optional, later: mem0 for agent-private notes** (e.g. "this repo's tests need `-p no:cacheprovider`").
   Use it only if agents start rediscovering the same facts every task. Configure it with:
   * `llm.provider="langchain"` (reuse aamt's model);
   * `embedder.provider="fastembed"` (local);
   * `vector_store.provider="pgvector"`;
   * `MEM0_TELEMETRY=False`.

   Keep it out of decisions and contracts.
4. **Revisit Graphiti** only if you need multi-hop questions over code entities and people ("which
   services depend on the auth decision that changed last sprint?"), and the bank already runs
   Neo4j.

### 3.6 Semantic recall

* **Gap found ✅.** `recall()` only scores vectors for records that FTS or entity matching already
  found. A paraphrase with no shared words is never a candidate. Example: "how do users sign in?"
  against "Authentication uses HS256 JWT bearer tokens…" returns `[]`.
* **Adopt ✅.** fastembed `BAAI/bge-small-en-v1.5` (384-d, ~6 ms/record on CPU) with a sqlite-vec
  `vec0` KNN table. The auth decision came back first (cosine 0.574). sqlite-vec is already in
  aamt's environment through `langgraph-checkpoint-sqlite`, and FTS5 is compiled into the same
  SQLite ✅.
* **Integrate, in `store.py`** (proposed):

  ```python
  # schema
  CREATE VIRTUAL TABLE IF NOT EXISTS records_vec USING vec0(embedding float[384] distance_metric=cosine);
  # rowid = records.rowid; write on put()/replace() when an embedder is configured

  def search_vector(self, qv, *, scopes, kinds, statuses, limit):
      rows = self._conn.execute(
          # `k = ?` not LIMIT: Python 3.11 bundles SQLite 3.40, which predates LIMIT push-down ✅
          "SELECT rowid, distance FROM records_vec WHERE embedding MATCH ? AND k = ?",
          (sqlite_vec.serialize_float32(qv), limit * 4)).fetchall()
      ...  # join to records, filter scope/kind/status in SQL, return (record, 1 - distance)
  ```

  In `memory.recall()`, add one more candidate source next to FTS and entities:

  ```python
  if self.embedder is not None and query.strip():
      for rec, sim in self.store.search_vector(self._query_vector(query), scopes=scopes,
                                               kinds=kinds, statuses=statuses, limit=pool):
          cands.setdefault(rec.id, (rec, 0.0))
  ```

  And a 6-line embedder in `integrations/fastembed.py`:

  ```python
  class FastEmbedEmbedder:
      def __init__(self, model="BAAI/bge-small-en-v1.5", cache_dir=None):
          from fastembed import TextEmbedding
          self._m = TextEmbedding(model_name=model, cache_dir=cache_dir)
      def embed(self, texts): return [v.tolist() for v in self._m.embed(texts)]
  ```
* **Bank note.** fastembed downloads the model from Hugging Face on first use (~130 MB with the
  tokenizer ✅). Pre-stage it in an internal artefact store, point `cache_dir` at it, and set
  `HF_HUB_OFFLINE=1`.
* **Production.** Use `pgvector` in the Postgres store.

### 3.7 Extraction: meeting minutes, handoff reports, lessons

* **Today.** Line markers (`DECISION:`, `SUMMARY:`, …) plus `complete_json` for the optional LLM
  merge.
* **Adopt:**
  * `model.with_structured_output(MinutesSchema)` for minutes and lessons;
  * `create_agent(..., response_format=HandoffSchema)` for the developer's final report. The result
    lands in `state["structured_response"]`.

  Both use tool-calling or native JSON schema, so parsing and validation are upstream's job.
* **Keep** the marker parser as the fallback for models that ignore tool schemas. aamt already meets
  some on OpenRouter; see its malformed-tool-call guard in `base.py`.

### 3.8 Delegation and sub-agents (recursive teams, PDF §7–10)

* **Keep** `TaskBrief` / `derive_child_brief` / `HandoffReport`. They're the content contract: what
  a child is told and what it must return.
* **Adopt for execution:** DeepAgents `SubAgentMiddleware`.
  * `SubAgent` has `system_prompt`, `tools`, `model`, `middleware`, `permissions`,
    `response_format`, `mode` ☑.
  * `CompiledSubAgent` wraps any LangGraph graph as a delegate.
  * Sub-agents run context-isolated; only their final answer returns.

  Set `response_format` to the `HandoffReport` schema, and render the child's brief into its
  `system_prompt`.
* **Also take:** DeepAgents `RubricMiddleware` (beta). A grader sub-agent checks the result against
  a rubric before the agent may finish, which maps onto the PDF's adversarial review. Try it on the
  verify step.

### 3.9 Project memory in the system prompt (AGENTS.md style)

* **Adopt, with governance:** DeepAgents `MemoryMiddleware(backend, sources=["/memories/AGENTS.md"], system_prompt=…)`.
  * Generate that file from the governed store: active decisions, contracts and conventions.
  * The default `system_prompt` tells the agent to **edit its memory files itself** whenever it
    learns something ☑. That's right for a personal assistant and wrong for a bank.
  * Pass our own prompt ("read-only; propose changes with `propose_decision`").
  * Deny writes to `/memories/` with `permissions=[FilesystemPermission(...)]`.
  * Changes flow back only through `SharedMemory.remember(..., status=PROPOSED)` and a human accept.

### 3.10 Channels and meetings

* **Keep** `channels.py` and `meetings.py`. They're small, and nothing upstream does time-boxed
  meetings ending in minutes. AG2 v1's streams and MetaGPT's environment are framework-bound.
* **Production.** Put the channel tables on the same Postgres, or map channels to Redis Streams /
  Kafka consumer groups. A consumer group is a per-reader cursor.

### 3.11 Code context

* **Today.** `codemap.py` outlines Python files with `ast` and ranks them by term overlap.
* **Adapt Aider's repo map (Apache-2.0).** Keep the NOTICE and licence header in the vendored file.
* **Spike ✅.**
  * `tree-sitter-language-pack` + Aider's `*-tags.scm` queries extracted definitions and references
    from Python and JavaScript.
  * A ~15-line personalised PageRank ranked `src/auth.py` first for an auth task.
  * Don't pull `networkx`: its `pagerank` needs scipy.
* **Integrate.** A new `codemap_ts.py` with the same `CodeMap.rank()/render()` API:
  1. tags cached in our SQLite blob table, replacing Aider's `diskcache`;
  2. personalisation from the brief's ownership paths and entities;
  3. binary search on the token budget (Aider's approach);
  4. fall back to the `ast` outline when tree-sitter isn't installed.
* **Dependencies.** `tree-sitter-language-pack` (MIT, active); `grep-ast` only for its `TreeContext`
  renderer. It's stale (no release since 2025-05) but tiny, so vendor that too if you prefer.

### 3.12 Bank-grade guardrails

These are all already installed with aamt, except tracing.

| Concern (from the bank assessment) | Adopt | How |
|---|---|---|
| PII in tool output reaching the model or memory | `PIIMiddleware` ✅ (email redacted, IBAN masked in the spike). Built-ins: email, credit card (Luhn), IP, MAC, URL. Custom regex or callable detectors. Strategies: block / redact / mask / hash. | One instance per type, with `apply_to_input`, `apply_to_tool_results` and `apply_to_output`. For names and addresses, a Presidio detector callable returning `{"type","value","start","end"}` ☑. Apply the same detectors in `SharedMemory.remember` (a small `Redactor` hook we add), because memory writes don't pass through the model loop. |
| Canonical memory changing without a human | `HumanInTheLoopMiddleware(interrupt_on={"propose_decision": True})` + our `PROPOSED` status | Needs a checkpointer and the orchestrator's resume flow (LangGraph `interrupt`). |
| LLM-written shell commands on the host | `ShellToolMiddleware(workspace_root, execution_policy=DockerExecutionPolicy(network_enabled=False, read_only_rootfs=True, memory_bytes=…, cpus="1", user="1000:1000"), redaction_rules=[…])` ☑ | Replaces aamt's deny-list `run_command`. That's the teammates' change; on K8s, OpenHands' agent-server or llm-sandbox are the heavier alternatives. |
| Malformed tool calls, provider errors | `ModelRetryMiddleware`, `ModelFallbackMiddleware(first_model, *more)` | Complements aamt's malformed-output guard. |
| Runaway loops and cost | `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware(thread_limit, run_limit)` | Per task. |
| "What exactly did the model see?" | LangGraph checkpointer (`SqliteSaver` → `PostgresSaver`) + OTel tracing (`openinference-instrumentation-langchain` or OpenLLMetry → the bank's collector; or self-hosted Langfuse) + the summarisation offload file | Replaces our own view persistence. |
| Telemetry and egress | `LANGSMITH_TRACING=false`, `HF_HUB_OFFLINE=1` after pre-staging; `MEM0_TELEMETRY=False` / `GRAPHITI_TELEMETRY_ENABLED=false` if those are ever added | Set in the deployment, and assert at startup. |

## 4. Resulting architecture

```
aamt (teammates)                   aamt_context (yours: governed memory + briefs)      upstream (adopted)
────────────────                   ─────────────────────────────────────────────      ──────────────────
Orchestrator ─ task graph ─► develop
                               │  bridge.brief_for_task() ──► ContextEngine
                               │                               ├─ SharedMemory (policy, supersession, CAS) ◄─ sqlite-vec KNN / pgvector
                               │                               │                                           ◄─ fastembed (local)
                               │                               ├─ ContextAssembler, TaskBrief, manifests
                               │                               ├─ AttemptSummary (retries)
                               │                               ├─ Channels, MeetingRoom→minutes          ◄─ with_structured_output
                               │                               └─ CodeMap (Aider algorithm)               ◄─ tree-sitter-language-pack
                               ▼
               create_agent(model, tools, middleware=context.middleware())            ◄─ LangChain v1 (installed)
                 ├─ DeepAgents SummarizationMiddleware + compact tool (offload to artefacts dir)  ◄─ deepagents
                 ├─ ContextEditingMiddleware (clear old tool results)
                 ├─ PIIMiddleware × n, ModelRetry/Fallback, call limits, HITL on memory-writing tools
                 └─ later: FilesystemMiddleware eviction, SubAgentMiddleware, RubricMiddleware
```

## 5. Integration plan

Every step can ship on its own, and each keeps the aamt diff small.

1. **aamt `agents/base.py`.** This is the teammates' file, and only these lines change there.
   `AamtTaskContext.instrument()` changes to return `(tools, middleware)` instead of
   `(tools, pre_model_hook)`:

   ```python
   -from langgraph.prebuilt import create_react_agent
   +from langchain.agents import create_agent
   ...
   -        agent = create_react_agent(self._model(), tools)
   +        tools, middleware = context.instrument(tools, max_steps=max_steps) if context else (tools, [])
   +        agent = create_agent(self._model(), tools, middleware=middleware)
   ...
   -                config={"recursion_limit": max_steps * 2 + 4},
   +                config={"recursion_limit": max_steps * 10 + 20},  # safety net; the step cap is middleware
   ```

   **Why the recursion limit must change ✅.**
   * Middleware with `before_model`/`after_model` hooks become graph nodes, and each one costs a
     super-step. PII detectors and call limits are such middleware.
   * With two PII detectors and a call limit, **12 model calls took 95 super-steps**. aamt's current
     budget (`max_steps * 2 + 4`) would stop that run at 28 with `GraphRecursionError`.
   * Cap the agent semantically with `ModelCallLimitMiddleware(run_limit=max_steps,
     exit_behavior="end")`, which is in the stack below. Keep `recursion_limit` only as a loose
     safety net.

   Regenerate `integration/aamt-context.patch`. It gets smaller: no hook plumbing.
2. **`aamt_context/integrations/middleware.py` (new).** One place that imports DeepAgents, so a
   version bump touches one file.

   ✅ This exact function was executed with scripted models, with `window=4_000`, IBAN + email
   detectors, and `extra=[ModelCallLimitMiddleware(run_limit=…, exit_behavior="end")]`:
   * 12 model calls and 3 summaries;
   * the summary was visible to the model, and 5 messages were sent instead of the full log;
   * the IBAN and email were redacted in tool results.

   Other orderings weren't tested.

   ```python
   from functools import partial
   from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware, ModelRetryMiddleware, PIIMiddleware
   from langchain_core.messages.utils import count_tokens_approximately
   from deepagents.backends import FilesystemBackend
   from deepagents.middleware.summarization import SummarizationMiddleware, SummarizationToolMiddleware

   # extra: e.g. [ModelCallLimitMiddleware(run_limit=max_steps, exit_behavior="end")] (see step 1)
   def build_middleware(*, summary_model, artifacts_dir, window=32_000, pii=(), extra=()):
       count = partial(count_tokens_approximately, use_usage_metadata_scaling=True)
       offload = FilesystemBackend(root_dir=artifacts_dir, virtual_mode=True)   # outside the git workspace
       summ = SummarizationMiddleware(
           summary_model, backend=offload, token_counter=count,
           trigger=("tokens", int(window * 0.75)), keep=("tokens", int(window * 0.25)),
           truncate_args_settings={"trigger": ("tokens", window // 2), "keep": ("messages", 12), "max_length": 2_000})
       return [
           summ, SummarizationToolMiddleware(summ),
           ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=window // 2, keep=3,
                                                             exclude_tools=("read_stored_output",))],
                                    token_counter=count),
           *(PIIMiddleware(name, detector=det, strategy="redact", apply_to_tool_results=True) for name, det in pii),
           ModelRetryMiddleware(max_retries=2),
           *extra,
       ]
   ```

   Also in this step:
   * `AamtTaskContext.instrument()` returns `wrap_tools(...)` (clipper only) plus
     `build_middleware(...)`;
   * `finish_run()` / `record_attempt()` read the final `state["messages"]` as today (attempts need
     no hook);
   * pin `deepagents==0.7.20` in a `[project.optional-dependencies] harness` extra.
3. **Semantic recall** (§3.6):
   * `records_vec` table and `search_vector`;
   * candidate source in `recall()`;
   * `integrations/fastembed.py`;
   * `embeddings = ["fastembed>=0.8,<0.9"]` extra;
   * a regression test with the paraphrase case.
4. **Structured extraction** (§3.7): minutes, lessons and handoff via schemas, with the marker
   parser as fallback.
5. **Repo map** (§3.11): `codemap_ts.py`, extra `codemap = ["tree-sitter-language-pack>=1.20"]`.
6. **Guardrails** (§3.12): PII detectors for bank identifiers, HITL on memory-writing tools, call
   limits, OTel. The Docker shell policy is the teammates' change.
7. **Delete** the retired modules once the new path has run a full sprint with parity. Parity means
   the same tasks pass, prompt tokens are no worse, and there are no lost-context failures in retro.

## 6. Risks of adopting, and mitigations

| Risk | Mitigation |
|---|---|
| DeepAgents is pre-1.0 and releases several times a week | Pin exact versions (the lockfile) and import it only in `integrations/middleware.py`. Keep [`spikes/spike_middleware.py`](spikes/spike_middleware.py) as a contract test run on every bump. Never override its private methods (e.g. `_build_new_messages_with_path`). |
| DeepAgents pulls `langchain-google-genai` + `google-genai` even if unused | Accept (+12 packages ✅), or vendor only `middleware/summarization.py` + its backend (MIT) if the bank's dependency review objects. |
| LangChain marks some internals "remove in 2.0" ☑ | Stay on the public constructors used above; re-run the spike on each upgrade. |
| Duplicate tool names silently resolve ✅ | Assert unique names at agent build time. |
| Middleware hooks add graph super-steps ✅ (12 model calls → 95 steps with 3 hooked middleware) | Cap steps with `ModelCallLimitMiddleware`, and raise `recursion_limit` to a safety net (§5 step 1). |
| Summaries are LLM-written and can drop facts | The offload file is the ground truth. Keep it with the run's audit trail under the retention policy. Decisions never live only in a summary: they go to the governed store. |
| sqlite-vec is pre-1.0, and old SQLite lacks LIMIT push-down ✅ | Use `k = ?`. Production runs on pgvector anyway. |
| Model downloads (fastembed, spaCy for Presidio) | Pre-stage the models internally and run offline. |
| Telemetry defaults in libraries we might add later (mem0, Graphiti) | Set the environment variables above, plus a startup check that fails if they're unset. |

## 7. Why not the others: short version with evidence

* **MetaGPT.** `uv` reports aamt + metagpt unsatisfiable on Python 3.11 and 3.12. Its last release
  (0.8.2, 2025-03) requires Python < 3.12, with 66 dependencies. The useful idea, watch/`cause_by`
  routing, is already in `channels.py`.
* **Letta.** `letta-ai/letta` is a landing page. The retired Python server lives on an `archive`
  branch, and its `AGENTS.md` forbids using it for production, comparisons, new integrations or
  copying. Current Letta is the TypeScript `letta-code` with an App Server.
* **OpenHands SDK.** It's the strongest open-source *runtime*: sandboxed workspaces,
  `ConfirmRisky`, and LLM, ensemble and policy-rail security analyzers. But:
  * it requires Python ≥ 3.12;
  * it adds 158 packages next to aamt and downgrades openai;
  * its condensers take its `View` of `Event`s and its LiteLLM `LLM` ☑, so they can't sit inside
    LangGraph.

  If the teammates want its sandbox, run it as the developer runtime and call it from aamt's develop
  node. Our memory then enters through its `AgentContext` and custom tools.
* **AG2 v1.** A good design: assembly policies, "compaction removes, aggregation creates", a
  `KnowledgeStore` with change subscriptions. But every piece takes AG2's `BaseEvent`,
  `MemoryStream` and `ConversationContext` ☑. It's a second framework, not a library.
* **pi.** TypeScript. It can run as a subprocess over RPC ☑, but it has no per-tool approval. Its
  own security doc says isolation must come from a container or VM ☑. Its compaction ideas are
  already in the design.
* **Agent Zero.** A Docker application with `requirements.txt` and no package ☑. Its ideas (history
  ratios, consolidation actions) are already in doc 03.
* **LangMem.** One release in 12 months, still at 0.0.30. Its summarisation node is superseded by
  the middleware above, and its memory managers are LLM-governed.
* **mem0 and Graphiti.** See §3.5. Both are good at their own job. Neither enforces who may change
  canonical memory. Both send telemetry by default ☑.
* **Claude Code.** Take the design from the public docs (done in doc 03). The leaked source is
  proprietary and must not be used in any product. The Claude Agent SDK is the licensed route, and
  only if Claude models are approved.

## 8. Reproduce

All from the repo root. The environment is aamt + aamt-context + the candidate libraries.

```bash
bash docs/spikes/resolve_with_aamt.sh 3.11
```

```bash
uv pip install "deepagents==0.7.20" fastembed grep-ast tree-sitter-language-pack
```

```bash
LANGSMITH_TRACING=false python docs/spikes/spike_middleware.py
```

```bash
python docs/spikes/spike_vectors.py
```

```bash
python docs/spikes/spike_repomap.py /path/to/aider-checkout
```

Versions used: deepagents 0.7.20, langchain 1.4.3, langchain-core 1.6.6, langgraph 1.2.12,
sqlite-vec 0.1.9, fastembed 0.8.1, tree-sitter-language-pack 1.20.0, grep-ast 0.9.0; Aider queries at
Aider-AI/aider HEAD of 2026-05-22.
