"""ContextSession: the working context of one agent run.

Owns the append-only :class:`~aamt_context.worklog.WorkingLog`, clips tool output on
the way in, runs the condenser pipeline before each model call, and persists every
entry, condensation and edit to the store so a run can be audited afterwards ("what
exactly did the model see at step 17?").

Harness-agnostic: a custom loop calls ``system/user/assistant/tool_result`` and
``view()``; an external loop (LangGraph's prebuilt agent) calls ``sync()`` with its own
messages and then ``view()``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ._util import Clock, system_clock
from .clipper import ToolOutputClipper
from .condensers import CondenseContext, CondenserPipeline, default_pipeline
from .config import BudgetConfig
from .llm import TextLLM
from .store import SqliteMemoryStore
from .tokens import TokenCounter, default_counter
from .worklog import Condensation, ContextEdit, Entry, EntryKind, ToolCall, WorkingLog, view_tokens


class ContextSession:
    def __init__(
        self,
        session_id: str,
        *,
        budget: BudgetConfig | None = None,
        counter: TokenCounter | None = None,
        llm: TextLLM | None = None,
        store: SqliteMemoryStore | None = None,
        pipeline: CondenserPipeline | None = None,
        clipper: ToolOutputClipper | None = None,
        clock: Clock | None = None,
        goal: str | None = None,
    ):
        self.session_id = session_id
        self.budget = budget or BudgetConfig()
        self.counter = counter or default_counter()
        self.llm = llm
        self.store = store
        self.pipeline = pipeline or default_pipeline()
        self.clipper = clipper if clipper is not None else ToolOutputClipper(
            store=store, counter=self.counter, max_tokens=self.budget.max_tool_result_tokens
        )
        self.clock = clock or (store.clock if store else system_clock)
        self.goal = goal
        self.log = WorkingLog()
        self.applied: list[str] = []
        self._persisted_conds = 0
        self._persisted_edits: set[str] = set()

    # ---------------------------------------------------------------- writes
    def add(self, entry: Entry) -> Entry:
        if self.log.append(entry) and self.store is not None:
            self.store.append_ctx(self.session_id, "entry", entry.id, entry.model_dump(mode="json"))
        return entry

    def system(self, text: str, **kw: Any) -> Entry:
        return self.add(Entry(kind=EntryKind.SYSTEM, content=text, ts=self.clock(), **kw))

    def user(self, text: str, **kw: Any) -> Entry:
        if self.goal is None:
            self.goal = text.strip().splitlines()[0] if text.strip() else None
        return self.add(Entry(kind=EntryKind.USER, content=text, ts=self.clock(), **kw))

    def assistant(self, content: str = "", tool_calls: Sequence[ToolCall] = (), **kw: Any) -> Entry:
        return self.add(Entry(kind=EntryKind.ASSISTANT, content=content, tool_calls=list(tool_calls),
                              ts=self.clock(), **kw))

    def tool_result(self, call_id: str, name: str, content: str, *, clip: bool = True, **kw: Any) -> Entry:
        meta = dict(kw.pop("meta", {}) or {})
        if clip:
            res = self.clipper.clip(name, content, session_id=self.session_id)
            content = res.text
            if res.clipped:
                meta.update(clipped=True, original_tokens=res.original_tokens, blob_id=res.blob_id)
        return self.add(Entry(kind=EntryKind.TOOL, tool_call_id=call_id, name=name, content=content,
                              meta=meta, ts=self.clock(), **kw))

    def clip_output(self, name: str, content: str) -> str:
        """Clip without logging — for harnesses that log tool results themselves."""
        return self.clipper.clip(name, content, session_id=self.session_id).text

    def sync(self, entries: Iterable[Entry]) -> int:
        """Append entries not seen yet (matched by id). Returns how many were new."""
        n = 0
        for e in entries:
            if not self.log.has(e.id):
                self.add(e)
                n += 1
                if e.kind is EntryKind.USER and self.goal is None:
                    self.goal = e.content.strip().splitlines()[0] if e.content.strip() else None
        return n

    # ----------------------------------------------------------------- reads
    def _protected(self) -> int:
        """Leading entries never forgotten: the system prompt(s) and the first user turn."""
        view = self.log.view()
        for i, e in enumerate(view):
            if e.kind is EntryKind.USER:
                return i + 1
            if e.kind not in (EntryKind.SYSTEM,):
                return i
        return len(view)

    def condense(self, *, force: bool = False, focus: str | None = None) -> list[str]:
        store = self.store
        ctx = CondenseContext(
            counter=self.counter, budget=self.budget, llm=self.llm, protected=self._protected(),
            force=force, focus=focus, goal=self.goal,
            blob_sink=(lambda name, text: store.put_blob(text, session_id=self.session_id,
                                                         meta={"tool": name, "masked": True}))
            if store is not None else None,
        )
        applied = self.pipeline.condense(self.log, ctx)
        self.applied += applied
        self._persist_reductions()
        return applied

    def view(self, *, condense: bool = True) -> list[Entry]:
        if condense:
            self.condense()
        return self.log.view()

    def tokens(self) -> int:
        return view_tokens(self.log.view(), self.counter)

    def raw_tokens(self) -> int:
        return view_tokens(self.log.entries, self.counter)

    def read_stored_output(self, blob_id: str, *, offset: int = 0, length: int = 6_000) -> str:
        """Page through an output this session stored. Outputs owned by other sessions
        (other agents' tool results) are reported as unknown."""
        if self.store is not None:
            owner = self.store.blob_session(blob_id)
            if owner is not None and owner != self.session_id:
                return f"ERROR: unknown blob_id {blob_id!r}"
        return self.clipper.read(blob_id, offset=int(offset), length=int(length))

    def stats(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "entries": len(self.log.entries),
            "view_entries": len(self.log.view()),
            "raw_tokens": self.raw_tokens(),
            "view_tokens": self.tokens(),
            "condensations": len(self.log.condensations),
            "edits": len(self.log.edits),
            "applied": list(self.applied),
        }

    # ---------------------------------------------------------- persistence
    def _persist_reductions(self) -> None:
        if self.store is None:
            return
        for cond in self.log.condensations[self._persisted_conds:]:
            self.store.append_ctx(self.session_id, "condensation", cond.id, cond.model_dump(mode="json"))
        self._persisted_conds = len(self.log.condensations)
        for edit in self.log.edits.values():
            if edit.id not in self._persisted_edits:
                self.store.append_ctx(self.session_id, "edit", edit.id, edit.model_dump(mode="json"))
                self._persisted_edits.add(edit.id)

    @classmethod
    def load(cls, store: SqliteMemoryStore, session_id: str, **kw: Any) -> ContextSession:
        """Rebuild a session from the store (audit / replay). Loading does not re-persist."""
        sess = cls(session_id, store=store, **kw)
        for item in store.ctx_items(session_id):
            payload = item["payload"]
            if item["kind"] == "entry":
                sess.log.append(Entry.model_validate(payload))
            elif item["kind"] == "condensation":
                sess.log.add_condensation(Condensation.model_validate(payload))
            elif item["kind"] == "edit":
                sess.log.add_edit(ContextEdit.model_validate(payload))
        sess._persisted_conds = len(sess.log.condensations)
        sess._persisted_edits = {e.id for e in sess.log.edits.values()}
        return sess
