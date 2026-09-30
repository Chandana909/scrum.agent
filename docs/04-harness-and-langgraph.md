# 04 — The harness question, and why LangGraph's control isn't granular enough here

## 1. What "harness" means

The *harness* is everything around the model call: how the prompt is assembled, how tool calls are
executed and their results fed back, when to stop, what to do when the context overflows, how
progress is reported, how state is persisted and resumed. Context management lives inside the
harness — it's the code that decides, before every model call, *what the model sees*.

aamt has two layers of harness:

* the **outer** task workflow — `prepare → develop ⟲ verify → commit | escalate`, a LangGraph
  `StateGraph` (`runtime/task_graph.py`);
* the **inner** agent loop — `langgraph.prebuilt.create_react_agent(model, tools)` invoked once per
  attempt (`agents/base.py:63`), i.e. model ⇄ tools until the model stops calling tools.

## 2. The short answer

LangGraph can express almost anything, so "not granular enough" needs to be precise:

> LangGraph's unit of control is the **node / super-step** and the **reducer-merged state channel**.
> Context management for this system needs control at finer grains — **every model call, every tool
> result, every message's presence in the model's view** — and at a different axis altogether —
> **governed memory shared between many agents over many runs**. The prebuilt ReAct agent aamt uses
> hides the inner loop entirely; LangGraph's own memory primitives (checkpointer, Store) are
> per-thread snapshots and a namespaced key-value store, with no notion of a view separate from the
> log, no token budgets, and no multi-writer governance. You *can* build all of that on LangGraph —
> but then LangGraph contributes the state machine and persistence, and every context decision is
> custom code anyway.

The rest of this document backs each part of that with evidence from the installed code
(langgraph 1.2.12, langgraph-prebuilt 1.1.0, langchain 1.4.3), and is honest about what LangChain v1
has fixed.

## 3. What LangGraph is good at (and why aamt should keep it where it helps)

* **Explicit workflows**: typed state, nodes, conditional edges — aamt's task graph is a clean
  example.
* **Durable execution**: a checkpointer snapshots state every super-step; runs resume after crashes;
  time travel for debugging.
* **Human-in-the-loop**: `interrupt()` pauses a graph for approval (the PRD's approval mode, the PDF's
  artifact verification gates).
* **Parallel fan-out**: `Send` map-reduce and parallel branches with reducers.
* **Ecosystem**: LangSmith tracing, LangGraph Platform, LangMem.

For the outer workflow — especially once checkpointing and interrupts are switched on — these are real
benefits. Nothing here recommends ripping LangGraph out.

## 4. Where the granularity mismatch is, point by point

### 4.1 The inner loop is a black box in aamt today

`create_react_agent(self._model(), tools)` is a compiled graph with two nodes (model, tools). aamt
passes no hooks, so between two model calls nothing in aamt runs: no token counting, no clipping, no
masking, no progress events per step (aamt emits one `AGENT_PROGRESS_REPORTED` per *attempt*), no
overflow recovery. The only budget is `recursion_limit = max_steps*2+4` (a count of super-steps, not
tokens). `create_react_agent` itself now warns that it is deprecated ("moved to `langchain.agents`…
to be removed in V2.0").

*What exists:* the prebuilt accepts `pre_model_hook`, which may return `llm_input_messages` — a view
used for the next model call without being written back to `messages`. **That hook is exactly how this
package integrates with aamt today** (`integrations/langchain.make_pre_model_hook`). And LangChain v1's
`create_agent` has middleware: `before_model`, `after_model`, `wrap_model_call`, `wrap_tool_call`
(`langchain/agents/middleware/types.py`). This package ships `ContextMiddleware` for it. So per-call
control is *available* — but only as hooks you fill with your own context engine.

### 4.2 No separation between the log and the view

The conversation *is* the `messages` state channel, merged with the `add_messages` reducer (append, or
replace by id); removing requires `RemoveMessage`. LangChain's own `SummarizationMiddleware.before_model`
returns

```python
{"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), <summary + kept messages>]}
```

i.e. it **rewrites the agent's state**: the live thread loses the raw transcript (it survives only in
older checkpoints, if a checkpointer is configured). OpenHands and pi show the alternative — an
append-only log plus condensation/edit events, with the model's input computed as a *projection*.
`ContextEditingMiddleware` does edit only the view (`request.override(messages=…)`), but its
placeholder is a bare `"[cleared]"` with no way to get the content back. LangMem's summarisation node
also returns a view (`llm_input_messages`) and keeps a `RunningSummary` in state. All of these are
single-agent, single-thread, and none records *why* something was removed.

This package's `WorkingLog`/`ContextSession` supplies the missing layer: log vs view, restorable stubs,
persisted condensation/edit events, replay.

### 4.3 Invariants are not modelled

Removing messages from a ReAct transcript must never separate a tool call from its result (providers
reject that), and multiple tool calls in one assistant turn must be kept or dropped together.
LangGraph validates the history before a model call (`_validate_chat_history` in the prebuilt) but
offers no "safe cut points" to condensers. OpenHands encodes these as view *properties* with
manipulation indices; pi as cut-point rules. Here: `worklog.cut_points` + `repair()`.

### 4.4 Memory primitives are per-thread snapshots and a KV store — not shared, governed memory

* **Checkpointer**: state snapshot per super-step, per thread. The SQLite saver serialises the whole
  checkpoint (all channel values, including the full message list) into one blob per checkpoint
  (`langgraph/checkpoint/sqlite/__init__.py`, `put`), so a long ReAct run costs roughly quadratic
  storage if checkpointing is enabled. It is a recovery mechanism, not a knowledge store.
* **`BaseStore`**: `(namespace tuple, key) → dict`, optional vector index, TTL. It has no versions or
  compare-and-swap, no proposals/curation, no supersession or validity intervals, no history, no
  subscriptions (an agent can't be *notified*; it must poll), and a write to the Store is not atomic
  with a write to graph state.
* **Parallel branches** merge into shared channels through reducers (append, last-write-wins, custom).
  Two agents that decide incompatible API shapes produce two values and a reducer that doesn't know
  they conflict. The conflict detection in this package (subject-keyed records, conflict proposals,
  challenges) has no LangGraph counterpart.

### 4.5 No budgets, no accounting, no cache discipline

There is no notion of a token budget for a node, a section, an agent or a sprint; nothing reports
"what share of this call was memory vs transcript". Prompt caching depends on a stable prefix;
middleware that rewrites history invalidates it, and nothing in the framework reasons about that
(Manus' #1 lesson; the Claude API's `clear_at_least`). Here: `BudgetConfig`, section caps, hysteresis
in masking, manifests.

### 4.6 Recursive delegation has no contract

Subgraphs are compiled ahead of time; dynamic sub-agents go through `Send` or tools that invoke other
graphs. What the child sees and what comes back are whatever state keys happen to overlap (subgraph
state sharing is implicit through the schema). The PDF's recursive orchestration needs an explicit
contract — brief down, bounded report up, decisions as proposals — which is application logic
(`briefs.py`, `engine.record_handoff`).

### 4.7 Meetings are not graph-shaped

Time-boxed, multi-party, timestamped conversations whose output is structured minutes are event- and
timer-driven. A graph can simulate turn-taking, but the transcript, the time box and the extraction
into memory are application logic (`meetings.py`).

### 4.8 Observability of context

LangSmith shows the inputs of each LLM call when tracing is on. It does not know which *memory records*
were injected, which were dropped for budget, or which messages were masked and why. Here: manifests
per brief, persisted condensations/edits per run, record access counters.

## 5. Harness options for aamt's developer agents

| Option | Per-call context control | Transcript model | Shared memory | Works with OpenRouter/Qwen/Mistral | Effort to adopt | Verdict |
|---|---|---|---|---|---|---|
| **A. Keep `create_react_agent` + `pre_model_hook`** (this package's patch) | full, via the hook | log in graph state, view from `ContextSession` | from `aamt_context` | yes | done (patch) | **Phase 1** — minimal change, immediate benefit; but deprecated API |
| **B. LangChain v1 `create_agent` + `ContextMiddleware`** | full, via `wrap_model_call` | same | same | yes | small (swap constructor) | good if the team wants to stay in the LangChain ecosystem |
| **C. `aamt_context.harness.AgentLoop`** (pi-style, ~150 lines) | native, every step | `ContextSession` is the log | same | yes (LangChain model adapter or any client) | small–medium (port aamt's malformed-tool-call guard, events) | **recommended Phase 2** for developer agents: removes the deprecated dependency, per-step progress events, overflow recovery, one place for all context decisions |
| D. OpenHands software-agent-sdk | strong (condensers, event log) | append-only events | its own; no team governance | yes (LiteLLM) | large (different workspace/tool model) | great reference; too big a swap |
| E. Claude Agent SDK | strong (Claude Code runtime: compaction, sub-agents, memory) | managed | files (CLAUDE.md, memory) | **no** — Claude models only, needs the Claude Code runtime | medium | mismatch with the OpenRouter/open-model setup |
| F. OpenAI Agents SDK *(background)* | sessions, handoffs, guardrails | sessions | none built in | yes via LiteLLM | medium | no advantage for this problem |
| G. AG2 v1 | assembly policies, compaction/aggregation, hub channels | event streams | `KnowledgeStore` + hub | yes | large (new framework) | best conceptual match for multi-agent memory; adopting means re-platforming |
| H. pi | excellent | session tree | extensions | yes | n/a (TypeScript) | design reference only |

## 6. Recommendation

> **Revised in [08](08-adopt-adapt-keep.md).** This table predates the adopt-vs-build check. The check
> added DeepAgents (LangChain's Claude-Code-style harness) and verified its middleware on aamt's
> dependency set.
>
> * Option B wins, with **upstream** middleware instead of our `ContextMiddleware`: DeepAgents
>   `SummarizationMiddleware` and LangChain `ContextEditingMiddleware`, `PIIMiddleware`, call limits.
> * Option C (`AgentLoop`) becomes a fallback reference, no longer the plan.
> * Point 1 below still describes the current patch.

1. **Now (patch in `integration/`)**: keep aamt's LangGraph task graph and prebuilt agent; plug the
   context engine in through `pre_model_hook` and tool wrapping. Shared memory is fed by the event bus;
   briefs replace `_project_summary`; attempts are remembered across retries.
2. **Next**: move developer agents to `AgentLoop` (or `create_agent` + `ContextMiddleware` if staying on
   LangChain matters to the team). Keep the LangGraph *outer* workflow and turn on its checkpointer and
   `interrupt()` for the PRD's approval mode and the PDF's artifact gates — that's where LangGraph earns
   its place.
3. **When going parallel and recursive (PDF §7-10)**: run sub-agents as async workers (asyncio task
   groups or a small actor layer) that communicate through channels and shared memory, each with a
   `TaskBrief` and returning a `HandoffReport`. The memory layer is already concurrency-safe
   (compare-and-swap, cursors, append-only logs); LangGraph `Send` can express the fan-out, but the
   dynamic, long-lived, message-driven shape (and meetings) fits an event-driven runtime better.

The context engine is framework-agnostic on purpose: whichever harness wins, the memory, assembly and
compaction logic doesn't change.
