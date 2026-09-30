# aamt-context

Context management and shared memory for [aamt](https://github.com/Krishil-Parikh/scrum-master-agent),
the autonomous Agile multi-agent engineering team — built as a standalone package so it can be developed
here and integrated into aamt with a small, verified patch.

What it gives the team:

* **Shared memory** — typed, scoped records (decisions with rationale, API contracts, requirements,
  clarifications, handoffs, lessons, blockers, failed attempts, …) with curation (proposals → accept /
  reject), de-duplication, subject-based supersession with history, adversarial challenges, and
  scored retrieval (SQLite FTS5/BM25 + entities + importance + recency + scope proximity; optional
  embeddings).
* **Channels** — append-only messages with per-reader cursors (every reader gets a broadcast exactly
  once), inbox/team/project/meeting conventions, a drop-in replacement for aamt's `Mailbox`.
* **Meetings** — time-boxed, timestamped transcripts that close into minutes: decisions, changes to
  earlier decisions, answered and open questions, action items with owners, blockers, risks,
  assumptions — all written to memory.
* **Task briefs** — per-task context assembled from memory within a token budget: previous attempts and
  review feedback, upstream handoffs, decisions and contracts in force, requirements and conventions,
  clarifications, lessons, relevant code outlines, inbox — with a manifest of what was included.
* **Working-context compaction** — per agent run: restorable clipping of big tool outputs, masking of old
  results, structured checkpoint summaries (LLM or deterministic fallback), non-destructive log/view
  separation, audit trail.
* **Attempt memory**, **delegation contracts** (`TaskBrief` down / `HandoffReport` up, for recursive
  sub-agents), **retro lessons** with recurrence counts, and an optional minimal **agent loop**.

The core depends only on `pydantic`; LangChain/LangGraph and aamt adapters live in
`aamt_context.integrations`.

> **Where to download what:** [docs/09-where-to-get-it.md](docs/09-where-to-get-it.md) has the links,
> install commands and classes for the shared-memory and context-management pieces to adopt.
>
> **Building on open source.** [docs/08-adopt-adapt-keep.md](docs/08-adopt-adapt-keep.md) checks each
> project in the design notes for licence, maintenance, co-installability with aamt, and fit. The
> spikes are in `docs/spikes/`.
>
> * **Replace:** the per-run compaction layer (worklog views, condensers, `AgentLoop`) should give way
>   to LangChain v1 `create_agent` plus DeepAgents/LangChain middleware.
> * **Keep:** the governed shared memory, briefs, attempts, meetings and channels. No open-source
>   project covers them.
> * **Improve:** add sqlite-vec + fastembed semantic recall, structured-output extraction, and an
>   Aider-style tree-sitter repo map.

## Quick start

```python
from aamt_context import ContextEngine, HandoffReport, MemoryKind, ReaderContext, Scope, TaskBrief

engine = ContextEngine.open(".aamt/context.db")   # pass llm=... to enable LLM summaries/minutes

# The Scrum Master records a decision (a curator's write is active immediately)
engine.memory.remember(
    scope=Scope.project("P-1"), kind=MemoryKind.DECISION, author="scrum-master",
    subject="api.transport", title="Public API is REST/JSON",
    data={"rationale": "browser clients; no service-to-service calls yet"},
)

# A finished task hands off to the tasks that depend on it
engine.record_handoff(
    HandoffReport(task_id="T-1", agent="A-db", summary="users table + UserStore", files_changed=["src/store.py"]),
    ReaderContext(project_id="P-1", task_id="T-1", agent_id="A-db", team="database"),
)

# Context for the next task
reader = ReaderContext(project_id="P-1", task_id="T-2", agent_id="A-be", role="backend", team="backend")
brief = engine.build_brief(TaskBrief(objective="Implement POST /login", task_id="T-2", dependencies=["T-1"]), reader)
print(brief.text)        # task, upstream handoff, decisions in force, ...
print(brief.manifest())  # sections, tokens, record ids shown, what was dropped

# Working context for one agent run
session = engine.session("T-2#run1")
session.system("You are a backend engineer.")
session.user(brief.text)
# ... session.assistant(...) / session.tool_result(...) as the loop runs
view = session.view()    # what the model should see next (condensed when needed)
```

A meeting:

```python
room = engine.meetings
m = room.open("P-1", kind="requirements", title="Requirements & doubts",
              participants=["A-fe", "A-be"], time_box_s=900)
q = room.say(m, "A-fe", "QUESTION: What does GET /tasks return?")
room.say(m, "A-be", "ANSWER: a JSON list of {id, title, done}", reply_to=q.id)
room.say(m, "A-be", "DECISION: Use SQLite for the MVP because zero ops (subject: db.engine)")
outcome = room.close(m)  # minutes -> decision + clarification records + a minutes summary
```

Plugging a session into LangGraph's prebuilt agent (what the aamt patch does):

```python
from langgraph.prebuilt import create_react_agent
from aamt_context.integrations.langchain import make_pre_model_hook, wrap_tools

session = engine.session("T-2#run1")
agent = create_react_agent(model, wrap_tools(tools, session), pre_model_hook=make_pre_model_hook(session))
```

## Integrating with aamt

See [docs/06-integration-guide.md](docs/06-integration-guide.md). In short, from an aamt checkout at
`516a4be`:

```bash
uv add --editable ../scrum
```

```bash
git apply ../scrum/integration/aamt-context.patch
```

## Development

```bash
uv sync
```

```bash
uv run pytest
```

The dev group installs LangChain/LangGraph and aamt itself (pinned to the patched commit) so the
integration tests run against the real host code. 82 tests; no API keys or network needed.

If this folder is synced by OneDrive/Dropbox, keep the virtualenv outside it by setting
`UV_PROJECT_ENVIRONMENT` to a local path before `uv sync`.

## Layout

```
src/aamt_context/     core (types, store, memory, channels, meetings, lessons, assembly, briefs,
                      worklog, clipper, condensers, session, attempts, codemap, engine, harness)
src/aamt_context/integrations/   langchain.py, aamt.py
tests/                unit + LangGraph/LangChain + real-aamt integration tests
integration/          aamt-context.patch (verified against aamt 516a4be)
docs/                 the design notes — start at docs/README.md
docs/spikes/          reproducible checks behind docs/08 (co-install resolution, middleware, vectors, repo map)
```
