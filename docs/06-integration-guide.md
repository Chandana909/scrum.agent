# 06 — Integrating `aamt-context` into aamt

The patch `integration/aamt-context.patch` is made against aamt commit **516a4be** and was verified to
apply cleanly to a fresh clone. It changes 6 existing files (~60 lines) and adds one wiring module and
one test file.

## 1. Steps

From the root of your aamt checkout (adjust `../scrum` to where this package lives):

```bash
uv add --editable ../scrum
```

```bash
git apply ../scrum/integration/aamt-context.patch
```

```bash
uv run pytest
```

Expected: **65 passed, 4 failed**. The 4 failures are pre-existing at 516a4be and unrelated
(`tests/test_agent_base.py` builds a real OpenRouter model before its stub is reached, so it needs an
API key — see 01 §6 G10). The 4 new tests in `tests/test_context_integration.py` must pass.

When the package is pushed somewhere shared, replace the editable path with a git dependency
(`uv add "aamt-context @ git+https://…"`) or make it a uv workspace member.

## 2. What the patch changes

| File | Change |
|---|---|
| `src/aamt/context.py` (new) | `build_context_bridge(settings, store, bus)`: opens `.aamt/context.db`, budgets from settings, a lazily built summariser model (no API key needed to start), subscribes the bridge to the event bus |
| `src/aamt/config.py` | `enable_context_engine` (True), `context_window_tokens` (32 000), `context_brief_tokens` (6 000), `context_memory_tools` (False); `context_db_path` |
| `src/aamt/orchestrator/orchestrator.py` | builds the bridge; `project_summary=self._context_for(...)` (brief from shared memory, or the old summary when disabled); passes `context=` to `run_task`; writes `reports/decisions.md`; closes the engine |
| `src/aamt/runtime/task_graph.py` | optional `context` threaded into the nodes; failed/blocked/errored attempts recorded; retries get `context.retry_context(feedback)` |
| `src/aamt/agents/base.py` | optional `context`; `tools, hook = context.instrument(tools)`; `create_react_agent(..., pre_model_hook=hook)`; `context.finish_run(messages)` |
| `src/aamt/agents/developer.py` | passes `context`; the `## Project` heading becomes `## Team context` |
| `src/aamt/agents/scrum_master.py` | `DECISION_RECORDED` now carries `reason`, `decision_id`, `related_tasks` |
| `tests/test_context_integration.py` (new) | attempt memory across a verify failure; `BaseAgent.run` instrumentation; orchestrator briefs from shared memory; switch-off path |

Settings are environment-driven like the rest of aamt: `AAMT_ENABLE_CONTEXT_ENGINE`,
`AAMT_CONTEXT_WINDOW_TOKENS`, `AAMT_CONTEXT_BRIEF_TOKENS`, `AAMT_CONTEXT_MEMORY_TOOLS`. Set the window to
your model's real context length on OpenRouter (32 000 is deliberately conservative).

## 3. What changes at runtime

| Before | After |
|---|---|
| Every developer prompt has the same `## Project` summary | `## Team context`: previous attempts and review/merge feedback on this task, upstream tasks' handoffs, decisions and contracts in force (with reasons), requirements and conventions, answered clarifications, relevant lessons, outlines of relevant files, other open blockers, unread messages — within 6 000 tokens |
| Tool outputs up to 200 kB enter the transcript | outputs over ~2 000 tokens are clipped (head + tail + error lines) and stored; the agent gets a `read_stored_output` tool to page through them |
| The ReAct transcript grows until the provider errors | old tool results are masked, then old turns summarised into a checkpoint, before each model call; the full transcript remains in graph state and in `context.db` |
| A retry starts with only the verifier's feedback | a retry also gets a compressed summary of each earlier attempt (files changed, failing tests, errors, what the agent claimed) |
| Decisions/retros/messages are stored, never read by agents | events flow into typed, scoped, de-duplicated shared memory with supersession and proposals |
| — | new files: `.aamt/context.db`, `.aamt/reports/decisions.md` |

## 4. How the other components are affected

| Component | In the patch | Suggested follow-ups (not in the patch) |
|---|---|---|
| Orchestrator | wiring, briefs, decisions report | pass `engine.proposals(...)` to the Scrum Master each sprint so agent proposals get accepted/rejected |
| Task graph | attempt memory, retry context | switch on a checkpointer + `interrupt()` for approval mode (04 §6) |
| BaseAgent / DeveloperAgent | instrumentation via `pre_model_hook` | move to `AgentLoop` or `create_agent` + `ContextMiddleware` (04 §5, options B/C); port the malformed-tool-call guard |
| Scrum Master | richer decision events | build the sprint-goal prompt with open action items and top lessons (`build_brief(include=["lessons", ...])`); replace `_capacity_from_lessons`'s keyword rule with lesson support counts; record architecture decisions with `subject=` so later changes supersede them |
| Backlog planner | — | give it requirements and answered clarifications from memory; record stories as requirement records |
| Reviewer / LLM judge | review rejections are ingested as task feedback | add decisions, contracts and conventions to the review prompt ("architecture consistency" is on its checklist but it currently sees none) |
| Merge / integration | conflicts are ingested as task feedback | — |
| Standup / retrospective | ingested via events (standup summary, lessons, action items) | run ceremonies as `MeetingRoom` meetings once agents speak in them (PDF §13-15); give the retro memory summaries instead of the last 60 raw events |
| Messaging (`Mailbox`) | — (unused today) | replace `AgentManager.mailbox` with `ChannelMailbox` before agents start messaging (fixes the broadcast bug) |
| Reporting | `decisions.md` | render "Key decisions" and lessons (with recurrence) into the final report from memory |
| TUI / CLI | — | a memory panel (proposals awaiting the SM, recent decisions, context usage per call from manifests); `aamt memory search/show/accept/reject` |

## 5. Rollout plan

1. **Passive memory (the patch).** Briefs, clipping, condensation, attempt memory. Check: the offline
   suite passes; on a real run, `context.db` fills with decisions/handoffs/feedback, and `ctx_items`
   manifests show briefs under budget.
2. **Just-in-time tools.** `AAMT_CONTEXT_MEMORY_TOOLS=true` adds `memory_search`, `memory_read`,
   `memory_note` and an index section to briefs. Watch the malformed-tool-call rate on small models
   before keeping it on.
3. **Curation loop.** The Scrum Master reviews `engine.proposals()` each sprint; decisions get subjects;
   challenges are answered. Optionally a human approves architecture decisions (approval mode).
4. **Meetings.** Requirements and brainstorming meetings through `MeetingRoom`; participants prompted
   to use the markers; minutes become the PRD's Q&A / decisions / assumptions (PDF §1-4).
5. **Harness.** Developer agents on `AgentLoop`; then parallel sub-agents with `TaskBrief` /
   `HandoffReport` (PDF §7-10).

## 6. Measuring whether it helps

Run the same problem statements with `AAMT_ENABLE_CONTEXT_ENGINE=false` and `=true` (3+ runs each) and
compare, from aamt's own store/event log plus `context.db`:

* tasks DONE / planned per sprint; sprints to completion; escalations;
* attempts per DONE task; review rejections and merge conflicts per task (reopens);
* context size per model call and per brief (manifests, session stats); tokens per task (add
  `usage_metadata` from model responses to session entries to track real tokens);
* repeated questions (clarifications asked again), duplicate work, contract changes that broke a
  dependent task;
* which memories are used (`access_count`) and which never are (candidates for pruning).

These map to PRD research questions 5 (how much context), 7 (do retros improve the next sprint) and
9 (causes of coordination failure).

## 7. Operating it

* **Where things are**: `.aamt/context.db` (SQLite). Deleting it resets shared memory only;
  `bridge.sync_from_store()` re-imports the project, decisions and retros from aamt's store.
* **Inspecting**:

```python
from aamt_context import ContextEngine, MemoryKind, Visibility
e = ContextEngine.open(".aamt/context.db")
for r in e.store.query(kinds=[MemoryKind.DECISION]):
    print(r.status.value, r.title, r.data.get("rationale"))
print([r.title for r in e.proposals(Visibility.build("<project id>"))])
print(e.store.ctx_items("brief:<task id>", kinds=["manifest"])[-1]["payload"])
```

* **OneDrive / cloud-synced folders**: keep virtual environments out of synced folders, e.g.
  `UV_PROJECT_ENVIRONMENT=<some local path>` before `uv sync`.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: aamt_context` | run `uv add --editable <path>` in the aamt repo |
| Summaries look like bullet lists of files and errors | the summariser LLM isn't available (e.g. no OpenRouter key); the extractive fallback is working as designed |
| Briefs too long / too short | `AAMT_CONTEXT_BRIEF_TOKENS` |
| Condensation too early / late | `AAMT_CONTEXT_WINDOW_TOKENS`; finer knobs in `BudgetConfig` inside `aamt/context.py` |
| `bridge.errors` not empty | an event handler failed on unexpected payload; the event is skipped, the run continues |
