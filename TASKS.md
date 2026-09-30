# Integration tasks: shared memory and context management → aamt

Work top to bottom. Each task has:

* an owner: **you** (this repo) or **team** (a file in aamt, owned by a teammate);
* the change to make;
* how to know it's done.

Background:

* [docs/06](docs/06-integration-guide.md): what the patch changes.
* [docs/08](docs/08-adopt-adapt-keep.md): why each library was chosen.
* [docs/09](docs/09-where-to-get-it.md): download links and install commands.

Status: `[x]` done · `[ ]` to do

## Checks to run before every merge

```bash
uv sync --locked
```

```bash
uv run ruff check src tests docs/spikes
```

```bash
uv run pyright
```

```bash
uv run pytest
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs these on Linux and Windows with Python
3.11 and 3.12. It also applies the patch to aamt at the pinned commit and runs aamt's own test suite.

---

## Phase 0: this package is ready (done)

- [x] **0.1 Quality gates.**
  * 97 tests pass.
  * ruff 0.16.9 passes: its default rule set plus the blind-except rule.
  * pyright passes in standard mode: 0 errors.
  * CI covers Linux and Windows × Python 3.11 and 3.12.
- [x] **0.2 Patch verified** against aamt `516a4be` (aamt's current `main`).
  * It applies cleanly: 65 aamt tests pass.
  * The other 4 fail only because they call a live OpenRouter model. They fail the same way without
    the patch.
- [x] **0.3 Hardening** ([CHANGELOG.md](CHANGELOG.md)):
  * atomic multi-step writes;
  * no lost updates across threads or processes (tested with both);
  * failed events are retried;
  * replay-safe handoffs and messages;
  * project-scoped handoffs and per-session saved outputs;
  * schema versioning;
  * `build_brief` 57% faster.

## Phase 1: ship the current integration (no new libraries)

- [ ] **1.1 [you] Tag a release to pin.**

  ```bash
  git tag v0.2.0
  ```

  ```bash
  git push origin v0.2.0
  ```

  Done when the tag is visible on GitHub.
- [ ] **1.2 [team] Add the package to aamt.**

  ```bash
  uv add "aamt-context @ git+https://github.com/Chandana909/scrum.agent@v0.2.0"
  ```

  While both are in development, `uv add --editable ../scrum` works too. Done when
  `python -c "import aamt_context"` runs in aamt's environment.
- [ ] **1.3 [team] Apply the patch.**

  ```bash
  git apply --check path/to/scrum/integration/aamt-context.patch
  ```

  ```bash
  git apply path/to/scrum/integration/aamt-context.patch
  ```

  It edits 6 aamt files and adds `src/aamt/context.py` and `tests/test_context_integration.py`
  (details in docs/06). Done when aamt's tests pass apart from the 4 live-model tests in
  `tests/test_agent_base.py`.
- [ ] **1.4 [team] Turn it on in aamt's settings.**
  * `enable_context_engine=True`.
  * `context_db_path`: one database per project, e.g. `.aamt/context.db` in the project folder.
  * `context_window_tokens`: the model's real context window.
  * `context_brief_tokens`: the default is 6000.
  * `context_memory_tools`: start with `False`.

  Done when a run creates the database and `decisions.md`.
- [ ] **1.5 [you + team] Decide who can make decisions official.**
  * By default the curators are `"scrum-master"` and `"human"`. Decisions from the LLM Scrum Master
    therefore become active immediately.
  * If a human must approve them, use `ContextConfig(curators=("human",))`. The Scrum Master's
    decisions then become proposals, which a person accepts with
    `engine.memory.accept(record_id, "human")`.

  Done when the choice is written into aamt's README.
- [ ] **1.6 [you] Smoke-test with a real model**, on one small project end to end. Check three things:
  * `decisions.md` lists decisions with their reasons;
  * the database holds a brief manifest per task (`ctx_items` rows whose session is
    `brief:<task id>`), showing which memories each agent saw;
  * a retried task's prompt contains "Earlier attempts on this task".
- [ ] **1.7 [team] Route logs.** Failures the package survives are logged at WARNING under the
  `aamt_context` logger: LLM fallbacks, events that couldn't be ingested, failed embeddings. Send
  that logger to aamt's log file.
- [ ] **1.8 [you] Keep the patch current.** Whenever aamt's `main` moves:
  1. update `AAMT_COMMIT` in the CI workflow and the `aamt @ git+…@<commit>` line in
     `pyproject.toml`;
  2. re-apply the patch and resolve any conflicts;
  3. regenerate it with `git diff > integration/aamt-context.patch` in the aamt checkout;
  4. push, and check that CI is green.

## Phase 2: context management on maintained upstream code

Why: DeepAgents and LangChain now maintain the same compaction design (docs/08 §3.2), which
retires about 900 lines here. Links and install commands are in docs/09 §1.

- [ ] **2.1 [team] Switch the agent builder** in `src/aamt/agents/base.py` from `create_react_agent`
  to `create_agent`. The diff is in docs/08 §5 step 1. Also change the step budget:
  * cap steps with `ModelCallLimitMiddleware(run_limit=max_steps, exit_behavior="end")`;
  * raise `recursion_limit` to a loose safety net, e.g. `max_steps * 10 + 20`. Middleware add graph
    steps: 12 model calls took 95 steps in testing.
- [ ] **2.2 [you] Add `src/aamt_context/integrations/middleware.py`** with `build_middleware(...)`.
  The verified code is in docs/08 §5 step 2. Add the extra `context = ["deepagents==0.7.20"]` to
  `pyproject.toml`.
- [ ] **2.3 [you] Change `AamtTaskContext.instrument()`** to return `(tools, middleware)` instead of
  `(tools, pre_model_hook)`. Keep `wrap_tools` (clipping plus `read_stored_output`): its tool name
  doesn't clash with aamt's `read_file`.
- [ ] **2.4 [you] Refuse duplicate tool names** when the agent is built. `create_agent` silently
  keeps only one of two same-named tools (docs/08 §3.3).
- [ ] **2.5 [you] Add a contract test.** Turn `docs/spikes/spike_middleware.py` into
  `tests/test_middleware_contract.py`, skipped when deepagents isn't installed. Run it on every
  deepagents upgrade.
- [ ] **2.6 [you + team] Regenerate the patch and prove it on a real run.**
  * aamt's suite passes.
  * A long task stays under the context window.
  * The offloaded history files appear in the run's artifacts folder, outside the git workspace.
- [ ] **2.7 [you] Delete the replaced code** after one sprint with equal or better results. "Equal
  or better" means:
  * the same tasks pass verification;
  * prompt tokens per task are no higher;
  * the retro reports no lost-context failures.

  Delete:
  * `condensers.py`, `session.py`, `harness.py`;
  * the view machinery in `worklog.py`;
  * in `integrations/langchain.py`: `make_pre_model_hook`, `ContextMiddleware`,
    `messages_from_entries`, `LangChainChatModel`, `tool_from_langchain`;
  * their tests.

  Keep `Entry`, `trace.py` and `entries_from_messages`: attempt memory uses them.

## Phase 3: shared memory that searches by meaning

Why: today "how do users sign in?" doesn't find the decision "Authentication uses JWT bearer
tokens". Links and install commands are in docs/09 §2.

- [ ] **3.1 [you] `store.py`: add vector storage and search.**
  * Add a `records_vec` table (`vec0`, 384 dimensions, cosine), written or refreshed on
    `put`/`replace` when an embedder is set.
  * Add `search_vector(query_vector, scopes, kinds, statuses, limit)`, using `k = ?` rather than
    `LIMIT` (docs/09 §2.1).
  * sqlite-vec is already installed with aamt.
- [ ] **3.2 [you] `memory.recall()`:** add the vector search as a third source of candidates, next
  to full-text and entity matches.
- [ ] **3.3 [you] Add `integrations/fastembed.py`** with a `FastEmbedEmbedder` (6 lines, docs/09
  §2.2). Add the extra `embeddings = ["fastembed==0.8.1"]` to `pyproject.toml`.
- [ ] **3.4 [you] Add a test** for the paraphrase case from `docs/spikes/spike_vectors.py`, skipped
  when fastembed isn't installed.
- [ ] **3.5 [team] Make the model available offline.** Download it once into an internal cache,
  pass `cache_dir`, and set `HF_HUB_OFFLINE=1` in production.

## Phase 4: structured extraction into memory

- [ ] **4.1 [you] Meeting minutes:** use `model.with_structured_output(MeetingMinutes)` when the model
  supports tool calling. Keep the `DECISION:`/`ACTION:` marker parser as the fallback.
- [ ] **4.2 [you] Retro lessons:** the same for `LessonConsolidator`.
- [ ] **4.3 [team + you] Developer's final report:** use `create_agent(..., response_format=<handoff schema>)`,
  and have the bridge read `state["structured_response"]`. `HandoffReport.from_text` stays as the
  fallback.

## Phase 5: production (only when needed)

- [ ] **5.1 [you] Postgres backend** behind the same store interface: `tsvector` full-text search,
  `pgvector`, and `SELECT … FOR UPDATE` inside `mutate`. Needed when several machines must share
  one memory.
- [ ] **5.2 [you] Identity.** The bridge already takes `author` from aamt's agent ids, and the
  memory tools never let the model choose it. Keep `author="human"` for actions by an
  authenticated person only.
- [ ] **5.3 [you] Retention.** Delete `ctx_items` and `blobs` older than N days (transcripts and
  saved tool output). Keep `records` and `history` (the decision log).
- [ ] **5.4 [you] Choose a licence** for this repository. None is set, so by default nobody else may
  reuse the code.
