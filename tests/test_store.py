from __future__ import annotations

import pytest

from aamt_context.store import SqliteMemoryStore, VersionConflict
from aamt_context.types import (
    ChannelMessage,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    Scope,
)


def _rec(**kw) -> MemoryRecord:
    base = dict(
        scope=Scope.project("P1"), kind=MemoryKind.DECISION, title="Use REST for the public API",
        body="gRPC is not needed yet", provenance=Provenance(author="scrum-master"),
    )
    base.update(kw)
    return MemoryRecord(**base)


def test_roundtrip_preserves_fields(store):
    rec = _rec(entities=["T-abc123", "src\\api.py"], tags=["API", "api"], data={"rationale": "simple"})
    store.put(rec)
    got = store.get(rec.id)
    assert got == rec
    assert got.entities == ["src/api.py", "t-abc123"]      # normalised + sorted
    assert got.tags == ["api"]


def test_update_bumps_version_and_records_history(store, clock):
    rec = store.put(_rec())
    clock.advance(10)
    updated = store.update(rec.id, actor="scrum-master", status=MemoryStatus.SUPERSEDED)
    assert updated.version == 2 and updated.updated_at == clock.t
    ops = [h["op"] for h in store.history(rec.id)]
    assert ops == ["create", "update"]


def test_compare_and_swap_rejects_stale_writer(store):
    rec = store.put(_rec())
    stale = store.get(rec.id)
    store.update(rec.id, actor="a", title="Use REST (v2)")
    with pytest.raises(VersionConflict):
        store.replace(stale.model_copy(update={"title": "clobber"}), expected_version=stale.version, actor="b")
    with pytest.raises(VersionConflict):
        store.update(rec.id, actor="b", expected_version=1, title="also stale")
    assert store.get(rec.id).title == "Use REST (v2)"


def test_full_text_search_ranks_and_filters(store):
    a = store.put(_rec(title="Authentication uses JWT tokens", body="HS256, 1h expiry"))
    store.put(_rec(title="Pagination style", body="cursor based pagination for lists"))
    store.put(_rec(title="JWT refresh", body="refresh tokens rotate", scope=Scope.project("OTHER")))
    hits = store.search_text(["jwt", "tokens"], scopes=[Scope.project("P1")])
    assert [r.id for r, _ in hits] == [a.id]
    assert hits[0][1] > 0


def test_search_reindexes_on_content_update_only(store):
    rec = store.put(_rec(title="Use sqlite"))
    store.update(rec.id, actor="x", title="Use postgres")
    assert not store.search_text(["sqlite"])
    assert [r.id for r, _ in store.search_text(["postgres"])] == [rec.id]
    store.touch([rec.id])  # counters only: no FTS churn, still searchable
    assert store.get(rec.id).access_count == 1
    assert store.search_text(["postgres"])


def test_query_by_entities_tags_subject(store):
    a = store.put(_rec(entities=["T-1"], tags=["api"], subject="api.transport"))
    store.put(_rec(title="other", entities=["T-2"]))
    assert [r.id for r in store.query(entities_any=["t-1"])] == [a.id]
    assert [r.id for r in store.query(tags_any=["API"])] == [a.id]
    assert [r.id for r in store.query(subject="api.transport")] == [a.id]
    assert store.query(scopes=[]) == []


def test_source_key_and_claim_are_idempotent(store):
    store.put(_rec(source_key="event:E-1"))
    assert store.get_by_source_key("event:E-1") is not None
    assert store.claim("event:E-9") is True
    assert store.claim("event:E-9") is False


def test_links_and_blobs_and_ctx_items(store):
    a, b = store.put(_rec()), store.put(_rec(title="b"))
    store.link(b.id, a.id, "supersedes")
    assert store.links(b.id) == [(b.id, a.id, "supersedes")]
    assert store.links(a.id, direction="in") == [(b.id, a.id, "supersedes")]
    blob = store.put_blob("x" * 10, session_id="s1", meta={"tool": "read_file"})
    assert store.get_blob(blob) == ("x" * 10, {"tool": "read_file"})
    assert store.append_ctx("s1", "entry", "e1", {"k": 1}) == 1
    assert store.append_ctx("s1", "entry", "e2", {"k": 2}) == 2
    assert [i["item_id"] for i in store.ctx_items("s1")] == ["e1", "e2"]


def test_cursors_only_move_forward(store):
    store.post_message(ChannelMessage(channel="project/P1", sender="a", content="hi"))
    store.set_cursor("b", "project/P1", 5)
    store.set_cursor("b", "project/P1", 2)
    assert store.get_cursor("b", "project/P1") == 5


def test_file_backed_store_persists(tmp_path, clock):
    path = tmp_path / "ctx" / "context.db"
    s1 = SqliteMemoryStore(path, clock=clock)
    rec = s1.put(_rec())
    s1.close()
    s2 = SqliteMemoryStore(path, clock=clock)
    assert s2.get(rec.id) == rec
    assert s2.search_text(["rest"])
    s2.close()
