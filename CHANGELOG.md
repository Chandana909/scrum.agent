# Changelog

## 0.2.0 (2026-09-30): hardening pass

### Fixed

- **Lost updates under concurrency.** Reinforcing, accepting, challenging and answering a record, and
  retiring a superseded one, all computed new values from an earlier read. Measured on the old
  code:
  * 8 threads reinforcing one lesson 200 times lost 175 increments;
  * 2 processes lost half of theirs.

  Derived updates now go through `store.mutate`, which recomputes them from the current row under
  the write lock.
- **Non-atomic multi-step writes.** Each of these could be left half-done by a failure:
  * a new decision and the retirement of the one it replaces;
  * accepting a proposal;
  * recording a handoff;
  * closing a meeting.

  Each now runs in one transaction.
- **Lost events.** Event ingestion marked an event processed *before* handling it, so a handler
  that failed (for example, database locked) lost the event for good. Events are now marked after
  success and retried on redelivery.
- **Replaying a handoff** re-added its decisions, inflating their support counts, and re-announced
  its contracts. It is now replay-safe. Channel messages accept a deterministic id
  (`CM-<event id>` for aamt events).
- **`latest_handoff`** matched task ids across projects. It is now scoped to the project.
- **Saved tool outputs** were readable by any session that knew the id. They are now private to the
  owning session.
- **Tool parameter types.** `tool()` reported `offset: int` and similar parameters as strings for
  functions defined in modules with postponed annotations. Models then sent strings, and the calls
  failed.
- **Search terms dropped non-ASCII letters** ("café" became "caf"). Terms now match SQLite's
  `unicode61` tokenizer in any script.

### Added

- `SqliteMemoryStore.transaction()`: nestable; inner blocks are savepoints.
- `SqliteMemoryStore.mutate()`.
- `SchemaVersionError`, backed by `PRAGMA user_version`.
- A `scope_prefix` filter and `order="recent"` in `query`.
- `get_vectors` (batched), `is_claimed` and `blob_session`.
- Context-manager support for the store and the engine.
- Logging for every failure the package survives. Swallowed exceptions are otherwise invisible.
- A `py.typed` marker.
- CI: lint, type check and tests on Linux and Windows × Python 3.11 and 3.12. A second job applies
  the patch to aamt and compares aamt's own tests with and without it, on Linux and Windows.
- ruff and pyright versions pinned in the dev group.
- 15 regression tests, one per fix above. They were confirmed to fail on the old code.

### Changed

- SQLite now runs with `synchronous=NORMAL` by default. This is safe with WAL; pass
  `synchronous="FULL"` for power-cut durability. There is also an explicit `busy_timeout_s`.
- Every write transaction takes the write lock up front (`BEGIN IMMEDIATE`).
- `build_brief` commits once instead of once per bookkeeping write.
- Performance, measured with 5,000 records:

  | Operation | Before | After |
  |---|---|---|
  | `build_brief` | 115 ms | 49 ms |
  | `recall` | 14 ms | 8 ms |
  | `remember` | 4.1 ms | 3.4 ms |
- `store.update()` no longer takes `retries`: updates are atomic, so there's nothing to retry.

## 0.1.0 (2026-09-29, untagged)

- First version: governed shared memory, channels, meetings, lessons, task briefs, attempt memory,
  working-context compaction, LangChain/LangGraph and aamt adapters, the aamt integration patch, and
  the design notes (docs/01–09).
