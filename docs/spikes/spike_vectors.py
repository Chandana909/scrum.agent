"""Spike: local embeddings (fastembed, ONNX) + KNN in SQLite (sqlite-vec) next to FTS5.

Shows the gap in aamt_context's recall today (vectors only re-rank lexical candidates)
and that a vec0 KNN candidate generator closes it, with zero network calls after the
one-time model download (~130 MB into FASTEMBED_CACHE_PATH or fastembed's default cache).

Run (from the repo root, in an env with aamt-context installed):
    uv pip install fastembed          # sqlite-vec already comes with langgraph-checkpoint-sqlite
    python docs/spikes/spike_vectors.py
Verified 2026-09-29, Windows / Python 3.11 (SQLite 3.40.1): recall() -> [] ; KNN -> auth decision first.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import time

import sqlite_vec
from fastembed import TextEmbedding

from aamt_context import (
    ContextConfig,
    Scope,
    SharedMemory,
    SqliteMemoryStore,
    Visibility,
)
from aamt_context.types import MemoryKind

CACHE = os.environ.get("FASTEMBED_CACHE_PATH")  # None -> fastembed default


class FastEmbedder:
    """aamt_context.memory.Embedder backed by a local ONNX model."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        self.model = TextEmbedding(model_name=model, cache_dir=CACHE)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.embed(texts)]


RECORDS = [
    (MemoryKind.DECISION, "Auth: JWT bearer tokens", "Authentication uses HS256 JWT bearer tokens issued by POST /login; expiry 1h."),
    (MemoryKind.DECISION, "Persistence: SQLite", "Tasks are stored in SQLite via the TaskStore class; no ORM."),
    (MemoryKind.CONTRACT, "GET /tasks", "GET /tasks returns [{id,title,done}] sorted by id."),
    (MemoryKind.LESSON, "Run tests before commit", "Always run pytest -q before committing; CI rejects red builds."),
    (MemoryKind.DECISION, "Frontend: React + Vite", "The web client is React 18 built with Vite."),
]


def main() -> None:
    t0 = time.perf_counter()
    emb = FastEmbedder()
    t_load = time.perf_counter() - t0
    project = Scope.project("demo")
    vis = Visibility.build("demo")
    mem = SharedMemory(SqliteMemoryStore(":memory:"), config=ContextConfig(), embedder=emb)
    for kind, title, body in RECORDS:
        mem.remember(scope=project, kind=kind, title=title, body=body, author="scrum-master")

    query = "how do users sign in?"   # no lexical overlap with the auth decision
    today = [s.record.title for s in mem.recall(query, vis, limit=3)]

    # sqlite-vec KNN over the same records (what the store would add as a candidate source)
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("CREATE VIRTUAL TABLE vec_records USING vec0(embedding float[384] distance_metric=cosine)")
    recs = mem.active(vis)
    t1 = time.perf_counter()
    vecs = emb.embed([f"{r.title}\n{r.body}" for r in recs])
    t_embed = (time.perf_counter() - t1) / len(recs)
    for i, v in enumerate(vecs):
        db.execute("INSERT INTO vec_records(rowid, embedding) VALUES (?, ?)", (i, struct.pack(f"{len(v)}f", *v)))
    qv = emb.embed([query])[0]
    rows = db.execute(
        # `k = ?` rather than LIMIT: Python 3.11's bundled SQLite (3.40) predates LIMIT push-down
        "SELECT rowid, distance FROM vec_records WHERE embedding MATCH ? AND k = 3 ORDER BY distance",
        (struct.pack(f"{len(qv)}f", *qv),),
    ).fetchall()
    knn = [(recs[rid].title, round(1 - dist, 3)) for rid, dist in rows]

    print(f"model load {t_load:.1f}s (cached after first run); embed ~{t_embed * 1000:.0f} ms/record on CPU")
    print("recall() today (FTS/entity candidates only):", today or "[] -> auth decision missed")
    print("sqlite-vec KNN candidates (title, cosine):", knn)


if __name__ == "__main__":
    main()
