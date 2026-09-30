"""SQLite persistence for shared memory, channels, working transcripts and blobs.

One database per project (same approach as aamt's ``project_state.db`` / ``events.db``):
WAL mode, one connection guarded by an RLock.

Invariants kept here rather than in callers:

* every write runs in a transaction that takes SQLite's write lock up front
  (``BEGIN IMMEDIATE``), so read-modify-write steps are atomic across threads *and*
  processes; :meth:`transaction` groups several writes into one atomic unit and nests
  (inner blocks become savepoints);
* records carry an integer ``version``; :meth:`replace` is compare-and-swap and
  :meth:`mutate` recomputes changes from the current row, so two agents updating the
  same record can neither overwrite each other nor lose an increment;
* every record change appends to ``history`` (who, what, when, full snapshot);
* full-text search (FTS5, BM25) is kept in sync by triggers on content columns only,
  so bumping access counters does not re-index;
* channel messages get a global ``seq``; each reader keeps its own cursor per channel,
  so one reader consuming a broadcast never hides it from another;
* the schema carries a version (``PRAGMA user_version``); a database written by a newer
  schema is refused instead of being misread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Literal, Self

from ._util import Clock, content_hash, dumps, new_id, normalize_entity, system_clock
from .types import (
    ChannelMessage,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    Trust,
)


class VersionConflict(RuntimeError):
    """A compare-and-swap update lost a race (the record changed since it was read)."""


class SchemaVersionError(RuntimeError):
    """The database was written by a newer, incompatible version of this package."""


SCHEMA_VERSION = 1

Synchronous = Literal["OFF", "NORMAL", "FULL", "EXTRA"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    rid              INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT NOT NULL UNIQUE,
    scope            TEXT NOT NULL,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL,
    subject          TEXT,
    title            TEXT NOT NULL,
    body             TEXT NOT NULL DEFAULT '',
    data             TEXT NOT NULL DEFAULT '{}',
    tags             TEXT NOT NULL DEFAULT '[]',
    entities         TEXT NOT NULL DEFAULT '[]',
    importance       REAL NOT NULL DEFAULT 0.5,
    confidence       REAL NOT NULL DEFAULT 1.0,
    pinned           INTEGER NOT NULL DEFAULT 0,
    support          INTEGER NOT NULL DEFAULT 1,
    author           TEXT NOT NULL,
    trust            TEXT NOT NULL,
    source_events    TEXT NOT NULL DEFAULT '[]',
    source_ref       TEXT,
    source_key       TEXT UNIQUE,
    sprint_id        TEXT,
    content_hash     TEXT NOT NULL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    valid_from       REAL NOT NULL,
    valid_to         REAL,
    superseded_by    TEXT,
    version          INTEGER NOT NULL DEFAULT 1,
    access_count     INTEGER NOT NULL DEFAULT 0,
    last_accessed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_records_scope_kind ON records(scope, kind, status);
CREATE INDEX IF NOT EXISTS ix_records_subject ON records(subject) WHERE subject IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_records_hash ON records(content_hash);

CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS ix_links_dst ON links(dst, rel);

CREATE TABLE IF NOT EXISTS history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL, version INTEGER NOT NULL, op TEXT NOT NULL,
    actor TEXT NOT NULL, ts REAL NOT NULL, note TEXT, snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_history_record ON history(record_id, seq);

CREATE TABLE IF NOT EXISTS vectors (
    record_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, vec TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]', meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, closed_at REAL
);
CREATE TABLE IF NOT EXISTS channel_messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE, channel TEXT NOT NULL, sender TEXT NOT NULL,
    recipients TEXT NOT NULL, type TEXT NOT NULL, content TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}', related TEXT NOT NULL DEFAULT '[]',
    reply_to TEXT, ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_channel_messages ON channel_messages(channel, seq);
CREATE TABLE IF NOT EXISTS cursors (
    reader TEXT NOT NULL, channel TEXT NOT NULL, seq INTEGER NOT NULL,
    PRIMARY KEY (reader, channel)
);

CREATE TABLE IF NOT EXISTS ctx_items (
    session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    item_id TEXT NOT NULL, payload TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS blobs (
    id TEXT PRIMARY KEY, session_id TEXT, meta TEXT NOT NULL DEFAULT '{}',
    content TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (key TEXT PRIMARY KEY, ts REAL NOT NULL);
"""

_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    title, body, tags, entities,
    content='records', content_rowid='rid', tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, title, body, tags, entities)
    VALUES (new.rid, new.title, new.body, new.tags, new.entities);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, title, body, tags, entities)
    VALUES ('delete', old.rid, old.title, old.body, old.tags, old.entities);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE OF title, body, tags, entities ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, title, body, tags, entities)
    VALUES ('delete', old.rid, old.title, old.body, old.tags, old.entities);
    INSERT INTO records_fts(rowid, title, body, tags, entities)
    VALUES (new.rid, new.title, new.body, new.tags, new.entities);
END;
"""

_COLS = (
    "id", "scope", "kind", "status", "subject", "title", "body", "data", "tags", "entities",
    "importance", "confidence", "pinned", "support", "author", "trust", "source_events",
    "source_ref", "source_key", "sprint_id", "content_hash", "created_at", "updated_at",
    "valid_from", "valid_to", "superseded_by", "version", "access_count", "last_accessed_at",
)


def _in(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _enum_values(values: Iterable[Any]) -> list[str]:
    return [getattr(v, "value", v) for v in values]


class SqliteMemoryStore:
    """Thread-safe (one connection + RLock) and multi-process-safe (WAL + write locks).

    ``synchronous="NORMAL"`` is SQLite's recommended setting for WAL: the database can't
    be corrupted, but a power cut may lose the last few commits. Pass ``"FULL"`` when
    every commit must survive a power cut. ``busy_timeout_s`` is how long a writer waits
    for another process's write lock before raising ``sqlite3.OperationalError``.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Clock = system_clock,
        synchronous: Synchronous = "NORMAL",
        busy_timeout_s: float = 30.0,
    ):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=busy_timeout_s)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        if synchronous not in ("OFF", "NORMAL", "FULL", "EXTRA"):
            raise ValueError(f"synchronous must be OFF, NORMAL, FULL or EXTRA, not {synchronous!r}")
        self._conn.execute(f"PRAGMA synchronous={synchronous}")
        found = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if found > SCHEMA_VERSION:
            self._conn.close()
            raise SchemaVersionError(
                f"{self.path} has schema version {found}; this aamt-context supports up to {SCHEMA_VERSION}"
            )
        self._conn.executescript(_SCHEMA)
        try:
            self._conn.executescript(_FTS)
            self.fts_enabled = True
        except sqlite3.OperationalError:  # SQLite built without FTS5
            self.fts_enabled = False
        if found < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    # ------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit of work. The outermost block takes the write lock
        (``BEGIN IMMEDIATE``) and commits; nested blocks are savepoints, so an inner
        failure that the caller handles only undoes the inner block."""
        with self._lock:
            depth = self._depth
            if depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._conn.execute(f"SAVEPOINT sp{depth}")
            self._depth = depth + 1
            try:
                yield self._conn
            except BaseException:
                self._depth = depth
                if depth == 0:
                    self._conn.rollback()
                else:
                    self._conn.execute(f"ROLLBACK TO sp{depth}")
                    self._conn.execute(f"RELEASE sp{depth}")
                raise
            self._depth = depth
            if depth == 0:
                self._conn.commit()
            else:
                self._conn.execute(f"RELEASE sp{depth}")

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        """Group several store calls into one atomic unit (all or nothing)::

            with store.transaction():
                store.put(new)
                store.update(old.id, ...)
        """
        return self._tx()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # records
    # ------------------------------------------------------------------
    @staticmethod
    def _to_row(r: MemoryRecord) -> tuple[Any, ...]:
        return (
            r.id, r.scope, r.kind.value, r.status.value, r.subject, r.title, r.body,
            dumps(r.data), dumps(r.tags), dumps(r.entities), r.importance, r.confidence,
            int(r.pinned), r.support, r.provenance.author, r.provenance.trust.value,
            dumps(r.provenance.source_events), r.provenance.source_ref, r.source_key,
            r.sprint_id, r.content_hash, r.created_at, r.updated_at, r.valid_from,
            r.valid_to, r.superseded_by, r.version, r.access_count, r.last_accessed_at,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"], scope=row["scope"], kind=MemoryKind(row["kind"]),
            status=MemoryStatus(row["status"]), subject=row["subject"], title=row["title"],
            body=row["body"], data=json.loads(row["data"]), tags=json.loads(row["tags"]),
            entities=json.loads(row["entities"]), importance=row["importance"],
            confidence=row["confidence"], pinned=bool(row["pinned"]), support=row["support"],
            provenance=Provenance(
                author=row["author"], trust=Trust(row["trust"]),
                source_events=json.loads(row["source_events"]), source_ref=row["source_ref"],
            ),
            source_key=row["source_key"], sprint_id=row["sprint_id"],
            content_hash=row["content_hash"], created_at=row["created_at"],
            updated_at=row["updated_at"], valid_from=row["valid_from"], valid_to=row["valid_to"],
            superseded_by=row["superseded_by"], version=row["version"],
            access_count=row["access_count"], last_accessed_at=row["last_accessed_at"],
        )

    def _history(self, c: sqlite3.Connection, r: MemoryRecord, op: str, actor: str, note: str | None) -> None:
        c.execute(
            "INSERT INTO history (record_id, version, op, actor, ts, note, snapshot) VALUES (?,?,?,?,?,?,?)",
            (r.id, r.version, op, actor, self.clock(), note, r.model_dump_json()),
        )

    def put(self, record: MemoryRecord, *, actor: str | None = None, note: str | None = None) -> MemoryRecord:
        with self._tx() as c:
            c.execute(
                f"INSERT INTO records ({','.join(_COLS)}) VALUES ({_in(_COLS)})", self._to_row(record)
            )
            self._history(c, record, "create", actor or record.author, note)
        return record

    def get(self, record_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return self._from_row(row) if row else None

    def get_many(self, record_ids: Sequence[str]) -> list[MemoryRecord]:
        if not record_ids:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM records WHERE id IN ({_in(record_ids)})", list(record_ids)
            ).fetchall()
        by_id = {r["id"]: self._from_row(r) for r in rows}
        return [by_id[i] for i in record_ids if i in by_id]

    def get_by_source_key(self, key: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM records WHERE source_key = ?", (key,)).fetchone()
        return self._from_row(row) if row else None

    def replace(
        self, record: MemoryRecord, *, expected_version: int, actor: str, op: str = "update",
        note: str | None = None,
    ) -> MemoryRecord:
        """Compare-and-swap write of a whole record. Bumps ``version`` and ``updated_at``."""
        new = record.model_copy(
            update={"version": expected_version + 1, "updated_at": self.clock()}, deep=True
        )
        new.content_hash = content_hash(new.kind.value, new.title, new.body)
        cols = [c for c in _COLS if c != "id"]
        values = self._to_row(new)[1:]
        with self._tx() as c:
            cur = c.execute(
                f"UPDATE records SET {', '.join(f'{col} = ?' for col in cols)} WHERE id = ? AND version = ?",
                (*values, new.id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict(f"{new.id}: expected version {expected_version}")
            self._history(c, new, op, actor, note)
        return new

    def mutate(
        self,
        record_id: str,
        fn: Callable[[MemoryRecord], dict[str, Any] | None],
        *,
        actor: str,
        op: str = "update",
        note: str | None = None,
        expected_version: int | None = None,
    ) -> MemoryRecord:
        """Atomic read-modify-write.

        ``fn`` receives the record as it is *now* and returns the fields to change
        (``None`` or ``{}`` leaves it untouched). The read and the write happen under the
        write lock, so changes derived from the current value — ``support + 1``, a merged
        ``data`` dict — can't lose a concurrent writer's update.
        """
        with self._tx():
            cur = self.get(record_id)
            if cur is None:
                raise KeyError(record_id)
            if expected_version is not None and cur.version != expected_version:
                raise VersionConflict(f"{record_id}: at version {cur.version}, expected {expected_version}")
            changes = fn(cur)
            if not changes:
                return cur
            new = cur.model_copy(update=changes, deep=True)
            return self.replace(new, expected_version=cur.version, actor=actor, op=op, note=note)

    def update(
        self, record_id: str, *, actor: str, op: str = "update", note: str | None = None,
        expected_version: int | None = None, **changes: Any,
    ) -> MemoryRecord:
        """Set fields to fixed values. For values derived from the current record, use
        :meth:`mutate`."""
        return self.mutate(record_id, lambda _cur: changes, actor=actor, op=op, note=note,
                           expected_version=expected_version)

    def query(
        self,
        *,
        scopes: Sequence[str] | None = None,
        kinds: Iterable[MemoryKind | str] | None = None,
        statuses: Iterable[MemoryStatus | str] | None = (MemoryStatus.ACTIVE,),
        subject: str | None = None,
        entities_any: Sequence[str] | None = None,
        tags_any: Sequence[str] | None = None,
        sprint_id: str | None = None,
        pinned: bool | None = None,
        author: str | None = None,
        as_of: float | None = None,
        scope_prefix: str | None = None,
        order: str = "updated",
        limit: int | None = None,
    ) -> list[MemoryRecord]:
        """``scope_prefix`` keeps records at that scope or below it (``p/P-1`` matches
        ``p/P-1`` and ``p/P-1/task/T-2`` but not ``p/P-10``). ``order="recent"`` is newest
        created first."""
        where, params = self._filters(
            scopes=scopes, kinds=kinds, statuses=statuses, subject=subject,
            entities_any=entities_any, tags_any=tags_any, sprint_id=sprint_id,
            pinned=pinned, author=author, as_of=as_of, scope_prefix=scope_prefix,
        )
        order_sql = {
            "updated": "updated_at DESC, rid DESC",
            "created": "created_at ASC, rid ASC",
            "recent": "created_at DESC, rid DESC",
            "importance": "importance DESC, support DESC, updated_at DESC",
        }[order]
        sql = "SELECT * FROM records" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += f" ORDER BY {order_sql}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _filters(
        *, scopes, kinds, statuses, subject=None, entities_any=None, tags_any=None,
        sprint_id=None, pinned=None, author=None, as_of=None, scope_prefix=None, alias: str = "",
    ) -> tuple[list[str], list[Any]]:
        p = f"{alias}." if alias else ""
        where: list[str] = []
        params: list[Any] = []
        if scopes is not None:
            scopes = list(scopes)
            if not scopes:
                return ["0"], []
            where.append(f"{p}scope IN ({_in(scopes)})")
            params += scopes
        if scope_prefix is not None:
            # prefix match on the path boundary; substr() avoids LIKE's wildcard escaping
            where.append(f"({p}scope = ? OR substr({p}scope, 1, ?) = ?)")
            params += [scope_prefix, len(scope_prefix) + 1, scope_prefix + "/"]
        if kinds is not None:
            ks = _enum_values(kinds)
            where.append(f"{p}kind IN ({_in(ks)})")
            params += ks
        if statuses is not None:
            ss = _enum_values(statuses)
            where.append(f"{p}status IN ({_in(ss)})")
            params += ss
        if subject is not None:
            where.append(f"{p}subject = ?")
            params.append(subject)
        if entities_any:
            ents = [normalize_entity(e) for e in entities_any]
            where.append(
                f"EXISTS (SELECT 1 FROM json_each({p}entities) je WHERE je.value IN ({_in(ents)}))"
            )
            params += ents
        if tags_any:
            tags = [t.lower() for t in tags_any]
            where.append(f"EXISTS (SELECT 1 FROM json_each({p}tags) jt WHERE jt.value IN ({_in(tags)}))")
            params += tags
        if sprint_id is not None:
            where.append(f"{p}sprint_id = ?")
            params.append(sprint_id)
        if pinned is not None:
            where.append(f"{p}pinned = ?")
            params.append(int(pinned))
        if author is not None:
            where.append(f"{p}author = ?")
            params.append(author)
        if as_of is not None:
            where.append(f"{p}created_at <= ? AND ({p}valid_to IS NULL OR {p}valid_to > ?)")
            params += [as_of, as_of]
        return where, params

    def search_text(
        self,
        query_terms: Sequence[str],
        *,
        scopes: Sequence[str] | None = None,
        kinds: Iterable[MemoryKind | str] | None = None,
        statuses: Iterable[MemoryStatus | str] | None = (MemoryStatus.ACTIVE,),
        limit: int = 50,
    ) -> list[tuple[MemoryRecord, float]]:
        """BM25 full-text search. Returns ``(record, relevance)``; higher is better."""
        terms = [t.replace('"', "") for t in query_terms if t.strip()]
        if not terms:
            return []
        where, params = self._filters(scopes=scopes, kinds=kinds, statuses=statuses, alias="r")
        if self.fts_enabled:
            match = " OR ".join(f'"{t}"' for t in terms)
            sql = (
                "SELECT r.*, bm25(records_fts, 5.0, 1.0, 2.0, 3.0) AS bm FROM records_fts "
                "JOIN records r ON r.rid = records_fts.rowid WHERE records_fts MATCH ?"
            )
            args: list[Any] = [match]
            if where:
                sql += " AND " + " AND ".join(where)
                args += params
            sql += " ORDER BY bm LIMIT ?"
            args.append(limit)
            with self._lock:
                rows = self._conn.execute(sql, args).fetchall()
            return [(self._from_row(r), -float(r["bm"])) for r in rows]
        # fallback: count matched terms with LIKE
        score_sql = " + ".join(
            "(CASE WHEN lower(r.title || ' ' || r.body || ' ' || r.tags || ' ' || r.entities) LIKE ? THEN 1 ELSE 0 END)"
            for _ in terms
        )
        inner = f"SELECT r.*, ({score_sql}) AS hits FROM records r"
        if where:
            inner += " WHERE " + " AND ".join(where)
        sql = f"SELECT * FROM ({inner}) WHERE hits > 0 ORDER BY hits DESC LIMIT ?"
        args = [f"%{t}%" for t in terms] + params + [limit]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [(self._from_row(r), float(r["hits"])) for r in rows]

    def find_duplicate(
        self, scope: str, kind: MemoryKind, digest: str,
        statuses: Iterable[MemoryStatus] = (MemoryStatus.ACTIVE, MemoryStatus.PROPOSED),
    ) -> MemoryRecord | None:
        ss = _enum_values(statuses)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM records WHERE scope = ? AND kind = ? AND content_hash = ? "
                f"AND status IN ({_in(ss)}) ORDER BY rid LIMIT 1",
                (scope, kind.value, digest, *ss),
            ).fetchone()
        return self._from_row(row) if row else None

    def touch(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        with self._tx() as c:
            c.execute(
                f"UPDATE records SET access_count = access_count + 1, last_accessed_at = ? "
                f"WHERE id IN ({_in(record_ids)})",
                (self.clock(), *record_ids),
            )

    def history(self, record_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, op, actor, ts, note, snapshot FROM history WHERE record_id = ? ORDER BY seq",
                (record_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # links -------------------------------------------------------------
    def link(self, src: str, dst: str, rel: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO links (src, dst, rel, created_at) VALUES (?,?,?,?)",
                (src, dst, rel, self.clock()),
            )

    def links(self, record_id: str, *, rel: str | None = None, direction: str = "out") -> list[tuple[str, str, str]]:
        clauses = {"out": "src = ?", "in": "dst = ?", "both": "(src = ? OR dst = ?)"}[direction]
        params: list[Any] = [record_id] * (2 if direction == "both" else 1)
        if rel:
            clauses += " AND rel = ?"
            params.append(rel)
        with self._lock:
            rows = self._conn.execute(f"SELECT src, dst, rel FROM links WHERE {clauses}", params).fetchall()
        return [(r["src"], r["dst"], r["rel"]) for r in rows]

    # vectors -----------------------------------------------------------
    def get_vector(self, record_id: str, digest: str) -> list[float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT content_hash, vec FROM vectors WHERE record_id = ?", (record_id,)
            ).fetchone()
        if not row or row["content_hash"] != digest:
            return None
        return json.loads(row["vec"])

    def get_vectors(self, digests: dict[str, str]) -> dict[str, list[float]]:
        """Vectors for ``{record_id: content_hash}`` in one query; stale ones are omitted."""
        if not digests:
            return {}
        ids = list(digests)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT record_id, content_hash, vec FROM vectors WHERE record_id IN ({_in(ids)})", ids
            ).fetchall()
        return {r["record_id"]: json.loads(r["vec"]) for r in rows if digests.get(r["record_id"]) == r["content_hash"]}

    def put_vector(self, record_id: str, digest: str, vec: Sequence[float]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO vectors (record_id, content_hash, vec) VALUES (?,?,?) "
                "ON CONFLICT(record_id) DO UPDATE SET content_hash = excluded.content_hash, vec = excluded.vec",
                (record_id, digest, json.dumps(list(vec))),
            )

    # ------------------------------------------------------------------
    # channels
    # ------------------------------------------------------------------
    def ensure_channel(
        self, channel_id: str, *, kind: str, title: str = "", participants: Sequence[str] = (),
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO channels (id, kind, title, participants, meta, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (channel_id, kind, title, dumps(list(participants)), dumps(meta or {}), self.clock()),
            )
        channel = self.get_channel(channel_id)
        assert channel is not None
        return channel

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["participants"] = json.loads(out["participants"])
        out["meta"] = json.loads(out["meta"])
        return out

    def update_channel(
        self, channel_id: str, *, meta: dict[str, Any] | None = None, closed_at: float | None = None,
        participants: Sequence[str] | None = None,
    ) -> None:
        with self._tx() as c:   # read inside the write lock: concurrent meta merges can't lose keys
            current = self.get_channel(channel_id)
            if current is None:
                raise KeyError(channel_id)
            merged = {**current["meta"], **(meta or {})}
            c.execute(
                "UPDATE channels SET meta = ?, closed_at = COALESCE(?, closed_at), participants = ? WHERE id = ?",
                (dumps(merged), closed_at, dumps(list(participants if participants is not None
                                                      else current["participants"])), channel_id),
            )

    def post_message(self, msg: ChannelMessage) -> ChannelMessage:
        """Append ``msg``. Idempotent by ``msg.id``: re-posting an id returns the stored message."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO channel_messages (id, channel, sender, recipients, type, content, data, "
                "related, reply_to, ts) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (msg.id, msg.channel, msg.sender, dumps(msg.recipients), msg.type, msg.content,
                 dumps(msg.data), dumps(msg.related), msg.reply_to, msg.ts),
            )
            if cur.rowcount == 1 and cur.lastrowid is not None:
                return msg.model_copy(update={"seq": int(cur.lastrowid)})
        existing = self.get_message(msg.id)
        if existing is None:  # pragma: no cover - the conflicting row can't vanish under the write lock
            raise RuntimeError(f"message {msg.id} was neither inserted nor found")
        return existing

    @staticmethod
    def _msg_from_row(row: sqlite3.Row) -> ChannelMessage:
        return ChannelMessage(
            seq=row["seq"], id=row["id"], channel=row["channel"], sender=row["sender"],
            recipients=json.loads(row["recipients"]), type=row["type"], content=row["content"],
            data=json.loads(row["data"]), related=json.loads(row["related"]),
            reply_to=row["reply_to"], ts=row["ts"],
        )

    def messages(self, channel: str, *, after_seq: int = 0, limit: int | None = None) -> list[ChannelMessage]:
        sql = "SELECT * FROM channel_messages WHERE channel = ? AND seq > ? ORDER BY seq"
        params: list[Any] = [channel, after_seq]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._msg_from_row(r) for r in rows]

    def get_message(self, message_id: str) -> ChannelMessage | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM channel_messages WHERE id = ?", (message_id,)).fetchone()
        return self._msg_from_row(row) if row else None

    def get_cursor(self, reader: str, channel: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT seq FROM cursors WHERE reader = ? AND channel = ?", (reader, channel)
            ).fetchone()
        return int(row["seq"]) if row else 0

    def set_cursor(self, reader: str, channel: str, seq: int) -> None:
        """Advance ``reader``'s cursor; never moves backwards."""
        with self._tx() as c:
            c.execute(
                "INSERT INTO cursors (reader, channel, seq) VALUES (?,?,?) "
                "ON CONFLICT(reader, channel) DO UPDATE SET seq = MAX(seq, excluded.seq)",
                (reader, channel, seq),
            )

    # ------------------------------------------------------------------
    # working-context items (transcripts, condensations, edits, manifests)
    # ------------------------------------------------------------------
    def append_ctx(self, session_id: str, kind: str, item_id: str, payload: dict[str, Any]) -> int:
        with self._tx() as c:   # the write lock makes MAX(seq)+1 and the insert one step
            c.execute(
                "INSERT INTO ctx_items (session_id, seq, kind, item_id, payload, ts) "
                "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ? FROM ctx_items WHERE session_id = ?",
                (session_id, kind, item_id, dumps(payload), self.clock(), session_id),
            )
            row = c.execute("SELECT MAX(seq) FROM ctx_items WHERE session_id = ?", (session_id,)).fetchone()
        return int(row[0])

    def ctx_items(self, session_id: str, *, kinds: Sequence[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT seq, kind, item_id, payload, ts FROM ctx_items WHERE session_id = ?"
        params: list[Any] = [session_id]
        if kinds:
            sql += f" AND kind IN ({_in(kinds)})"
            params += list(kinds)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY seq", params).fetchall()
        return [
            {"seq": r["seq"], "kind": r["kind"], "item_id": r["item_id"],
             "payload": json.loads(r["payload"]), "ts": r["ts"]}
            for r in rows
        ]

    def ctx_sessions(self, prefix: str = "") -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM ctx_items WHERE session_id LIKE ? ORDER BY session_id",
                (prefix + "%",),
            ).fetchall()
        return [r[0] for r in rows]

    # blobs (full tool outputs behind clipped observations) ---------------
    def put_blob(self, content: str, *, session_id: str | None = None, meta: dict[str, Any] | None = None) -> str:
        blob_id = new_id("BL")
        with self._tx() as c:
            c.execute(
                "INSERT INTO blobs (id, session_id, meta, content, created_at) VALUES (?,?,?,?,?)",
                (blob_id, session_id, dumps(meta or {}), content, self.clock()),
            )
        return blob_id

    def get_blob(self, blob_id: str) -> tuple[str, dict[str, Any]] | None:
        with self._lock:
            row = self._conn.execute("SELECT content, meta FROM blobs WHERE id = ?", (blob_id,)).fetchone()
        return (row["content"], json.loads(row["meta"])) if row else None

    # idempotency ---------------------------------------------------------
    def claim(self, key: str) -> bool:
        """Mark ``key`` processed. False if it already was (replayed event, resumed run)."""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO processed (key, ts) VALUES (?, ?)", (key, self.clock()))
            return cur.rowcount == 1

    def is_claimed(self, key: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM processed WHERE key = ?", (key,)).fetchone() is not None

    def blob_session(self, blob_id: str) -> str | None:
        """The session a stored output belongs to (``None`` if unknown or unowned)."""
        with self._lock:
            row = self._conn.execute("SELECT session_id FROM blobs WHERE id = ?", (blob_id,)).fetchone()
        return row["session_id"] if row else None
