# Notes: context management and shared memory for aamt

Read in order:

1. [01 — How aamt's agents are coded today](01-how-aamt-agents-work.md): the runtime, every LLM call
   and what it sees, what memory exists and who reads it, the gaps (with file:line references), and what
   the architecture PDF adds.
2. [02 — Requirements](02-requirements.md): functional and non-functional requirements, traced to the
   PDF/PRD and to the modules that implement them.
3. [03 — Methodologies and alternatives](03-methodologies-and-alternatives.md): component by
   component — memory model, retrieval, scoping, write governance, communication, meetings, context
   assembly, compaction, attempt memory, delegation, learning, code context, observability, safety —
   with what Claude Code, the Claude API, pi, OpenHands, MetaGPT, AG2, Agent Zero, mem0, Letta,
   Graphiti, LangGraph/LangChain/LangMem and Manus each do, and what was chosen here and why.
4. [04 — The harness question, and why LangGraph's control isn't granular enough here](04-harness-and-langgraph.md)
5. [05 — Architecture of `aamt_context`](05-architecture.md): layering, schema, lifecycle, flows,
   invariants, configuration, extension points, limits.
6. [06 — Integration guide](06-integration-guide.md): apply the patch, what changes, how each other
   component is affected, rollout, evaluation, operations.
7. [07 — References](07-references.md): every source with its URL and the exact commit read.
8. [08 — Adopt, adapt or keep](08-adopt-adapt-keep.md): which open-source pieces to take instead of
   building. For each project it gives licence, maintenance, whether it co-installs with aamt, and
   telemetry defaults, all verified. It then goes component by component with integration code, a
   step-by-step migration plan and risks. The spikes are in [`spikes/`](spikes/).

## The short version

* aamt persists decisions, messages and retros but **no agent ever reads them**: developers get the same
  project summary for every task, retries forget the previous attempt, the ReAct transcript is
  unbounded, and broadcast messages are consumed globally by the first reader.
* `aamt_context` adds **typed, scoped, governed shared memory** (decisions with rationale and
  supersession, contracts, clarifications, handoffs, lessons, attempts…), **per-reader channels**,
  **time-boxed meetings that end in minutes**, **budgeted task briefs**, **non-destructive transcript
  compaction** with restorable tool output, and **attempt memory** — framework-free, SQLite-backed,
  working without an LLM.
* It plugs into aamt with a verified patch (6 files, ~60 changed lines + a wiring module) through
  LangGraph's `pre_model_hook` and aamt's event bus.
* LangGraph controls nodes and state channels; this problem needs control per model call, per tool
  result and per message, plus multi-agent memory governance — which LangGraph leaves to you. Keep
  LangGraph for the outer workflow (checkpointing, approvals).
* **Revised by doc 08:** LangChain v1 middleware now provides per-call control inside the loop.
  DeepAgents ships non-destructive, offloading summarisation that you can run and verify here.
  * So don't own the inner loop. Use `create_agent` plus upstream middleware.
  * Own the part nobody else has: governed team memory, briefs and attempt memory.
