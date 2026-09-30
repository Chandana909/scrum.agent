# 01 — How aamt's agents are coded today (and what that means for context & memory)

All references are to `Krishil-Parikh/scrum-master-agent` at commit **516a4be** (the version this
package's integration patch targets). Paths are relative to that repo.

---

## 1. The big picture

aamt is a single Python process with SQLite persistence and a LangGraph state machine for one
task at a time.

```
CLI (typer, cli.py) / TUI (Textual, tui/app.py)
   │
   ▼
Orchestrator (orchestrator/orchestrator.py)
   │  create_project → bootstrap (team + backlog) → [plan sprint → execute sprint → review → retro] × N
   │
   ├── ScrumMaster (agents/scrum_master.py)      deterministic planning + small LLM calls
   │     ├── BacklogPlanner (planning/backlog_planner.py)   LLM → BacklogPlan JSON (3 fallbacks)
   │     └── plan_sprint (planning/sprint_planner.py)       deterministic scoring, no LLM
   │
   ├── run_task (runtime/task_graph.py)          LangGraph StateGraph, one task end-to-end
   │     prepare → develop ⟲ verify → commit | escalate
   │                 │
   │                 └── DeveloperAgent.implement (agents/developer.py)
   │                        └── BaseAgent.run (agents/base.py)
   │                               └── langgraph.prebuilt.create_react_agent(model, tools)   ← the ReAct loop
   │                                      tools = build_developer_toolset(Workspace)         (tools/toolset.py)
   │
   ├── ReviewerAgent (integration/reviewer.py)   one LLM call over the diff
   ├── integrate_branch (integration/merge.py)   git merge + regression tests + rollback
   ├── run_standup / run_retrospective (ceremonies/)
   └── reports (reporting/reports.py)

Persistence
   ProjectStore (state/store.py)  SQLite, one table per entity, pydantic JSON documents
   EventBus     (events/bus.py)   SQLite, append-only events, synchronous subscribers
   (no LangGraph checkpointer is used anywhere)
```

Key facts that shape the context/memory design:

* **Execution is sequential.** `execute_sprint` loops over tasks in topological order
  (`orchestrator.py:144`). `max_concurrent_agents = 3` exists in config (`config.py:78`) but is not
  used. The architecture PDF wants parallel and recursive agents, so memory must be concurrency-safe
  from the start even though today's runtime is single-threaded.
* **Only the developer agent runs a multi-step loop.** Every other LLM use is a single call.
* **The Scrum Master is not an LLM agent loop.** It is deterministic Python (planner, sprint scoring,
  standup, review) that makes three small LLM calls (goal sentence, standup summary, retro JSON).

---

## 2. The life of one task, step by step

1. **Planning** — `ScrumMaster.plan_sprint` scores eligible tasks (`sprint_planner.py`), assigns an
   agent per task, moves tasks to `ASSIGNED`, records a `Decision` ("Committed N tasks…") and emits
   `SPRINT_PLANNED`.
2. **Dispatch** — `Orchestrator.execute_sprint` picks the next task whose dependencies are `DONE`
   (unmet dependencies → `BLOCKER_CREATED` and skip), then calls
   `run_task(task, …, project_summary=self._project_summary(project))` (`orchestrator.py:171-179`).
3. **prepare** node — ensures a git repo, creates branch `task/<id>`, records `base_commit`, moves the
   task to `IN_PROGRESS`.
4. **develop** node — builds a fresh `DeveloperAgent(role)` and calls
   `implement(task, ws, project_summary=…, extra_context=state["last_feedback"])`
   (`task_graph.py:145-151`).
5. **Inside `BaseAgent.run`** (`base.py:54-125`):
   * `create_react_agent(self._model(), tools)` (`base.py:63`) — LangGraph's prebuilt ReAct graph;
   * invoked once with `[SystemMessage(role contract), HumanMessage(render_task_context(...))]`
     and `recursion_limit = max_steps*2 + 4` (`base.py:67-70`; `max_steps` defaults to 40);
   * the loop runs until the model answers without tool calls or the limit is hit;
   * the result is parsed: count AI turns, list tool names, take the last tool-less AI message as the
     summary; detect "malformed" tool calls leaked as text (`[TOOL_CALLS]`, `<tool_call>`, …,
     `base.py:101-117`); `claims_blocked` is a keyword check on the summary (`base.py:31-33`).
6. **verify** node — `verify_task` (runtime/verification.py) checks: a diff exists vs `base_commit`;
   the test command passes (or no tests are needed for scaffolding/docs tasks); every acceptance
   criterion with a `check` command exits 0; optionally an LLM judge grades check-less criteria.
7. **retry** — on failure and attempts left, `last_feedback = report.feedback_for_agent()`
   (`task_graph.py:244`) and control returns to **develop**, which builds a **new** agent and a **new**
   ReAct loop whose only memory of the previous attempt is that feedback string.
8. **commit** — `commit_all`, evidence recorded, task → `DONE`, events `COMMIT_CREATED`,
   `TASK_COMPLETED`. **escalate** — task → `BLOCKED`, `ESCALATED_TO_HUMAN`.
9. Back in the orchestrator: events are emitted to the bus (`emit_dict`), the reviewer runs on the
   diff (rejection → task `REOPENED` with the comments squeezed into a 300-char history note), the
   branch is merged with a regression run (failure → revert + `REOPENED`), and a standup runs after
   every task ("tick").
10. After the sprint: `review_sprint`, `run_retrospective`, `complete_sprint`, sprint report on disk.

---

## 3. How an agent is "coded"

An aamt agent is three things:

| Piece | Where | What it is |
|---|---|---|
| `AgentSpec` | `models/agent.py` | role id, title, capability keywords, system prompt, coordinator flag, optional model override |
| Role contract | `agents/roles.py` | `_DEVELOPER_CONTRACT` template (inspect first, smallest change, add tests, run tests, don't claim what you can't prove, stop if blocked, report files/criteria/tests, don't commit) formatted per role; `SCRUM_MASTER` spec |
| Runtime loop | `agents/base.py` | `BaseAgent.run(objective, tools, system_prompt, max_steps)` wraps `create_react_agent` |

The **team** is fixed: Scrum Master + backend, frontend, database, ml, qa, devops (`roles.py:59-86`).
`AgentManager` (`agents/manager.py`) keeps status/workload and picks the best agent by
(role match, capability overlap, lower load).

The **tools** a developer gets (`tools/toolset.py`): `list_files`, `repo_tree`, `read_file`,
`search_code`, `write_file`, `run_command`, `git_status`, `git_diff`, `run_test_suite`. All are
path-jailed to the task's workspace; shell commands go through a deny-list, secrets are stripped
from the environment, and network is off by default (`tools/workspace.py`).

The **models** come from `llm/provider.py:build_chat_model`: OpenRouter by default (Qwen 2.5 72B /
Mistral Small via a rotating key pool with fallbacks), or Anthropic/OpenAI/Azure/Ollama. Every
agent gets a fresh model object per run.

What an agent **does not** have today: any memory beyond its own single ReAct run, any view of other
agents' work except through the repository itself, and any mechanism to receive messages.

---

## 4. Every LLM call site and exactly what context it sees

| # | Call site | Model | Input it receives | Size controls | Shared memory used |
|---|---|---|---|---|---|
| 1 | `BacklogPlanner.generate` (`planning/backlog_planner.py`) | coordinator | system rules + problem, language, framework, testing, project acceptance criteria | up to 3 calls (structured → raw JSON → repair) | none |
| 2 | `ScrumMaster._goal_sentence` (`scrum_master.py:86-110`) | coordinator | problem statement + titles of this sprint's tasks | `max_tokens=120` | none |
| 3 | **Developer ReAct loop** (`base.py:63-70`) | developer | system: role contract. user: `render_task_context` = task id/title, priority, dependency ids, description, acceptance criteria (+check cmds), `## Project` = `_project_summary` (name, problem, language, framework, project acceptance), `## Additional context` = last verifier feedback. Then the whole growing transcript of tool calls and results. | **none** except `recursion_limit` (≈ 40 model turns). No token counting, no trimming. | only `_project_summary` |
| 4 | LLM judge `_llm_judge` (`verification.py:108-142`) | reviewer | task title/description, unchecked criteria, `diff[:10000]` | silent 10 kB cut | none |
| 5 | `ReviewerAgent.review` (`reviewer.py:50-92`) | reviewer | checklist + task + criteria + `diff[:12000]` | silent 12 kB cut | none (no conventions, no decisions) |
| 6 | Standup `_team_summary` (`ceremonies/standup.py:104-129`) | coordinator | one line per agent: yesterday/today/blockers | `max_tokens=180` | none |
| 7 | `run_retrospective` (`ceremonies/retrospective.py:42-99`) | coordinator | sprint goal, review counts, **last 60 events** (`retrospective.py:58`) | event cut at 60 | none |

The tool outputs that fill call #3's transcript are large:

* `read_file` returns up to **200 kB** per call (`workspace.py:147`);
* `run_command` renders up to **6 000 chars of stdout + 6 000 of stderr** (`workspace.py:36`);
* `run_test_suite` returns up to **12 000 chars** of output (`test_runner.py:75`), rendered with the
  last 8 000 (`test_runner.py:51`);
* `write_file` puts the **entire file content** into the assistant's tool-call arguments.

A 40-step run that reads a handful of files and runs the tests a few times easily exceeds a 32k
context — the default model (`qwen/qwen-2.5-72b-instruct`) is served with 32k–131k windows depending
on the OpenRouter provider. When it overflows the provider errors, `BaseAgent.run` returns
`ok=False`, the graph retries from scratch, and the same thing happens again.

---

## 5. What "memory" exists today and who reads it

| Store | Written by | Read by (at runtime) |
|---|---|---|
| `tasks` (with per-task `history`) | planner, SM, task graph, orchestrator | planner, standups (status changes), reports |
| `decisions` | `ScrumMaster.record_decision` (sprint plan, carry-forward) | **only the final report** (`reports.py:172`) |
| `messages` + `Mailbox` | nobody at runtime (`AgentManager.mailbox` exists, no caller) | nobody |
| sprint `review` / `retrospective` | SM, ceremonies | `_capacity_from_lessons` keyword check; reports |
| `standups` | standup engine | reports |
| event log | everyone | retrospective (last 60), reports, TUI |

So the "Persistent Decision Memory" and "Single Source of Truth" principles of the architecture PDF
exist as *storage* but not as *context*: nothing that is decided, learned or communicated reaches the
next agent's prompt.

---

## 6. Gaps and bugs that matter for context & shared memory

**G1. Developers see only the project summary.** `project_summary=self._project_summary(project)`
(`orchestrator.py:176`, defined at `:422`) is the same string for every task: no decisions, API
contracts, upstream task results, review comments, lessons or messages. The PRD's context
construction list (phased plan §5.2: "Task + Acceptance Criteria + Relevant Files + Architecture
Information + Dependencies + Relevant Decisions + Agent Role") is only partly implemented.

**G2. Retries throw away the trajectory.** `develop` builds a new agent each attempt and passes only
`last_feedback` (`task_graph.py:150`, `:244`). The next attempt re-explores the repository, often
re-makes the same change, and re-fails the same way.

**G3. The ReAct transcript is unbounded.** No token budget, no clipping, no masking, no
summarisation (see §4). The only limit is `recursion_limit` (`base.py:69`).

**G4. Broadcast messages are consumed globally.** `Mailbox.drain` sets `m.read = True` and saves it
(`messaging.py:55-58`); `list_messages(recipient=…)` returns `broadcast` messages to everyone
(`store.py:165`). The first agent to drain a broadcast marks it read *for all agents*. (Reproduced in
`tests/test_aamt_integration.py::test_aamt_mailbox_loses_broadcasts_channel_mailbox_does_not`.)

**G5. Retro learning is a keyword rule.** Retro action items only influence the next sprint if they
contain "reduce" and "scope"/"capacity" (`scrum_master.py:112-118`); `_carry_forward` records a
decision no agent ever reads (`orchestrator.py:323-334`). Recurring problems are not detected.

**G6. Decisions have no lifecycle.** `Decision` (`models/decision.py`) has no status, subject,
supersession or rationale-in-event (the `DECISION_RECORDED` event carries only `decision` and
`context`). Changing a decision means adding a second one; nothing says which is current. Operational
notes ("final report written to …", "resumed from persisted state") are recorded as decisions too.

**G7. Review feedback is lost on reopen.** A rejected review reopens the task with
`review.feedback()[:300]` in a history note; the next run of that task never sees it.

**G8. `create_react_agent` is deprecated** in LangGraph 1.x ("moved to `langchain.agents`… to be
removed in V2.0" — the warning shows in this package's test run), and no checkpointer is used, so a
crash mid-task loses the run.

**G9. Hidden truncations.** Reviewer `diff[:12000]`, judge `diff[:10000]`, retro `events[-60:]` cut
silently; a large change is reviewed on its first 12 kB only.

**G10. Repo hygiene.** README links `docs/ARCHITECTURE.md`, which is not in the repo; README says
"63 tests, no API key needed" but the 4 tests in `tests/test_agent_base.py` fail without an OpenRouter
key because `BaseAgent.run` builds the real model (`self._model()`) before the stubbed
`create_react_agent` is reached. (Baseline at 516a4be: 61 passed, 4 failed.)

---

## 7. What the architecture PDF adds, and what each part implies for memory

| PDF section | Requirement | Implication for context/memory |
|---|---|---|
| §1 Requirements meeting → PRD.md "captures questions, decisions, clarifications, assumptions, resolved ambiguities… prevents agents from repeatedly asking the same questions" | Q&A must be durable and retrievable | `clarification` records (question + answer, or open), `assumption`, `decision`; a "don't re-ask" section in briefs |
| §2 Skill layer (`skills/<domain>/<domain>-agent.md`) | per-domain expertise loaded by each agent | procedural memory; progressive disclosure (Claude Code / Letta skills pattern); team-scoped lessons are the seed of skills |
| §3-5 Brainstorm → technical-approach.md, design.md | team decisions with trade-offs and reasons | decisions with rationale + alternatives; artifacts as memory with approval status |
| §6 Human-in-the-loop verification of each artifact | nothing proceeds on an unapproved artifact | proposals vs accepted records; curator acceptance |
| §7-9 Hierarchical, recursive orchestration | Scrum Master → domain agent → sub-agents → … | scope chains (a sub-agent inherits ancestors' context, siblings are isolated); delegation briefs and bounded handoffs |
| §10 Dynamic work allocation | an idle agent helps another domain | context attaches to the *work item* (task scope, team scope), not to the agent |
| §11 Adversarial review | challenge decisions, record reasons | `challenge` → open question linked to the decision; curator answers; supersession keeps history |
| §12 Bottom-up verification | sub-agent → domain agent → SM → human | a child's decisions/interfaces arrive as proposals for the parent to accept |
| §13-15 Timed meetings, timestamped logs, SM extracts decisions/actions/blockers/dependencies/open questions/changes to previous decisions | meetings must not become forgotten chat | meeting channel + time box + transcript format + minutes extraction → typed records, including supersession of changed decisions |
| §16 Weekly/biweekly Business SME review | progress, decisions, deviations from PRD | roll-up summaries; decision log report |
| §19 Principles: single source of truth, persistent decision memory, time-bounded collaboration | — | the whole shared-memory layer |
| §20 Loop ends with "Update Project Memory → Repeat" | memory is part of the loop, not a side log | event-driven ingestion + curated writes every step |

The rest of the notes explain how each of these was designed, what the alternatives were, and why.
