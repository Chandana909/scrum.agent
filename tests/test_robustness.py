"""Regression tests for the hardening pass: atomicity, concurrency, replay safety, isolation."""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from aamt_context._util import terms
from aamt_context.briefs import HandoffReport
from aamt_context.channels import Channels
from aamt_context.engine import ContextEngine, ReaderContext
from aamt_context.harness import tool
from aamt_context.integrations.aamt import AamtContextBridge
from aamt_context.meetings import MeetingKind
from aamt_context.memory import SharedMemory
from aamt_context.store import SchemaVersionError, SqliteMemoryStore
from aamt_context.types import MemoryKind, MemoryRecord, MemoryStatus, Provenance, Scope

P = "P1"


def _rec(title: str, scope: str = "p/P1") -> MemoryRecord:
    return MemoryRecord(scope=scope, kind=MemoryKind.FACT, title=title, provenance=Provenance(author="a"))


# --------------------------------------------------------------------------- store
def test_transaction_is_all_or_nothing(store):
    with pytest.raises(RuntimeError), store.transaction():
        store.put(_rec("first"))
        store.put(_rec("second"))
        raise RuntimeError("boom")
    assert store.query() == []


def test_nested_failure_only_undoes_the_inner_block(store):
    with store.transaction():
        store.put(_rec("kept"))
        with pytest.raises(RuntimeError), store.transaction():
            store.put(_rec("undone"))
            raise RuntimeError("inner")
        store.put(_rec("also kept"))
    assert sorted(r.title for r in store.query()) == ["also kept", "kept"]


def test_mutate_derives_changes_from_the_current_row(store):
    rec = store.put(_rec("counter"))
    stale = store.get(rec.id)
    store.mutate(rec.id, lambda cur: {"support": cur.support + 1}, actor="x")
    # a caller holding a stale copy still increments the *current* value
    assert stale is not None and stale.support == 1
    assert store.mutate(rec.id, lambda cur: {"support": cur.support + 1}, actor="y").support == 3
    assert store.mutate(rec.id, lambda cur: None, actor="z").version == 3   # no-op writes nothing


def test_concurrent_reinforcement_loses_no_updates(memory):
    kw = {"scope": Scope.project(P), "kind": MemoryKind.LESSON, "author": "scrum-master",
          "title": "Run the tests before committing"}
    first = memory.remember(**kw).record

    def worker() -> None:
        for _ in range(25):
            memory.remember(**kw)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = memory.get(first.id)
    assert got is not None and got.support == 1 + 8 * 25


def _reinforce_in_process(path: str, n: int) -> None:
    mem = SharedMemory(SqliteMemoryStore(path))
    for _ in range(n):
        mem.remember(scope=Scope.project(P), kind=MemoryKind.LESSON, author="scrum-master",
                     title="Run the tests before committing")


def test_concurrent_processes_lose_no_updates(tmp_path):
    path = str(tmp_path / "shared.db")
    mem = SharedMemory(SqliteMemoryStore(path))
    first = mem.remember(scope=Scope.project(P), kind=MemoryKind.LESSON, author="scrum-master",
                         title="Run the tests before committing").record
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_reinforce_in_process, args=(path, 40)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    got = mem.get(first.id)
    assert got is not None and got.support == 1 + 2 * 40


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    SqliteMemoryStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionError):
        SqliteMemoryStore(path)


def test_scope_prefix_respects_path_boundaries(store):
    for scope in ("p/P-1", "p/P-1/task/T-1", "p/P-10", "p/P-10/task/T-1"):
        store.put(_rec(f"in {scope}", scope=scope))
    got = sorted(r.scope for r in store.query(scope_prefix="p/P-1"))
    assert got == ["p/P-1", "p/P-1/task/T-1"]


def test_post_message_is_idempotent_by_id(engine_fixture):
    hub = engine_fixture.channels
    first = hub.post("project/P1", "A-be", "hello", message_id="CM-ev-1")
    again = hub.post("project/P1", "A-be", "hello (replayed)", message_id="CM-ev-1")
    assert again.seq == first.seq and again.content == "hello"
    assert len(hub.history("project/P1")) == 1


@pytest.fixture
def engine_fixture(store, clock) -> ContextEngine:
    return ContextEngine(store, clock=clock)


# -------------------------------------------------------------------------- engine
def test_replayed_handoff_neither_reinforces_nor_reannounces(engine_fixture):
    eng = engine_fixture
    reader = ReaderContext(project_id=P, task_id="T-1", agent_id="A-be", role="backend", team="backend")
    report = HandoffReport(task_id="T-1", agent="A-be", summary="login endpoint",
                           decisions=["Tokens expire after 1h"], interfaces=["POST /login -> {token}"])
    eng.record_handoff(report, reader, source_key="event:E-1")
    replay = eng.record_handoff(report, reader, source_key="event:E-1")
    assert len(replay) == 1
    decisions = eng.store.query(kinds=[MemoryKind.DECISION], statuses=[MemoryStatus.PROPOSED, MemoryStatus.ACTIVE])
    assert [d.support for d in decisions] == [1]
    announced = [m for m in eng.channels.history(Channels.project(P)) if m.type == "API_CONTRACT_UPDATE"]
    assert len(announced) == 1


def test_latest_handoff_is_scoped_to_the_project(engine_fixture):
    eng = engine_fixture
    for pid, summary in (("P1", "P1 login"), ("P2", "P2 billing")):
        eng.record_handoff(HandoffReport(task_id="T-1", agent="A", summary=summary),
                           ReaderContext(project_id=pid, task_id="T-1", agent_id="A"))
    h1 = eng.latest_handoff("T-1", project_id="P1")
    h2 = eng.latest_handoff("T-1", project_id="P2")
    assert h1 is not None and "P1 login" in h1.title
    assert h2 is not None and "P2 billing" in h2.title


def test_meeting_close_is_atomic(engine_fixture, monkeypatch):
    rooms = engine_fixture.meetings
    m = rooms.open(P, kind=MeetingKind.REQUIREMENTS, title="Reqs", participants=["A-be"])
    rooms.say(m, "A-be", "DECISION: Use SQLite for the MVP (subject: db.engine)")
    real = engine_fixture.memory.remember

    def fail_on_summary(**kw):
        if kw.get("kind") is MemoryKind.SUMMARY:
            raise RuntimeError("disk full")
        return real(**kw)

    monkeypatch.setattr(engine_fixture.memory, "remember", fail_on_summary)
    with pytest.raises(RuntimeError):
        rooms.close(m)
    assert engine_fixture.store.query(kinds=[MemoryKind.DECISION]) == []   # nothing half-written
    assert rooms.get(P, m.id).ended_at is None                            # and still open
    monkeypatch.setattr(engine_fixture.memory, "remember", real)
    outcome = rooms.close(m)
    assert outcome.meeting.ended_at is not None
    assert len(engine_fixture.store.query(kinds=[MemoryKind.DECISION])) == 1


# ---------------------------------------------------------------------- isolation
def test_stored_outputs_are_private_to_their_session(engine_fixture):
    a = engine_fixture.session("T-1#a#run1")
    b = engine_fixture.session("T-2#b#run1")
    clipped = a.clipper.clip("read_file", "x" * 40_000, session_id=a.session_id)
    assert clipped.blob_id is not None
    assert "xxxx" in a.read_stored_output(clipped.blob_id, length=10)
    assert b.read_stored_output(clipped.blob_id).startswith("ERROR: unknown blob_id")


# ------------------------------------------------------------------------ ingestion
def test_failed_event_is_retried_on_redelivery(engine_fixture, monkeypatch):
    bridge = AamtContextBridge(engine_fixture)
    event = SimpleNamespace(id="E-9", type="DECISION_RECORDED", project_id=P, agent_id="scrum-master",
                            sprint_id=None, task_id=None,
                            payload={"decision": "Use JWT", "context": "architecture", "reason": "stateless"})
    real = engine_fixture.memory.remember
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(**kw)

    monkeypatch.setattr(engine_fixture.memory, "remember", flaky)
    assert bridge.ingest_event(event) is False and bridge.errors
    assert bridge.ingest_event(event) is True       # redelivery succeeds
    assert bridge.ingest_event(event) is False      # and is not processed twice
    assert [d.title for d in engine_fixture.store.query(kinds=[MemoryKind.DECISION])] == ["Use JWT"]


# --------------------------------------------------------------------------- misc
def test_tool_schema_types_survive_postponed_annotations():
    def read(blob_id: str, offset: int = 0, ratio: float = 1.0, full: bool = False) -> str:
        return blob_id

    read.__annotations__ = {"blob_id": "str", "offset": "int", "ratio": "float", "full": "bool", "return": "str"}
    props = tool(read).spec.parameters["properties"]
    assert {k: v["type"] for k, v in props.items()} == {
        "blob_id": "string", "offset": "integer", "ratio": "number", "full": "boolean"}


def test_terms_handle_non_ascii_text():
    assert terms("Café crème — 日本 déjà_vu") == ["café", "crème", "日本", "déjà", "vu"]
