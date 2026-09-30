"""The working transcript of one agent run: an append-only log and its projected view.

Nothing is ever deleted from the log. Context reduction is recorded as two kinds of
events, applied on read by :meth:`WorkingLog.view`:

* :class:`Condensation` — forget a contiguous range and insert a summary in its place
  (OpenHands' ``Condensation`` tombstone; pi's ``CompactionEntry``). The summary is a
  virtual entry with a deterministic id, so a later condensation can fold an earlier
  summary into a new one (summary of summaries).
* :class:`ContextEdit` — replace one entry's content (or its tool-call arguments), or
  omit it (pi's ``context_edit``). Used for masking old tool output.

The view always satisfies the provider invariants: every tool call has exactly one
result right after its assistant turn, and no result is orphaned.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ._util import new_id, system_clock
from .tokens import MESSAGE_OVERHEAD_TOKENS, TokenCounter


class EntryKind(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SUMMARY = "summary"


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Entry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("CE"))
    kind: EntryKind
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None   # TOOL entries: which call this answers
    name: str | None = None           # TOOL entries: tool name
    meta: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=system_clock)


class Condensation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("CD"))
    forgotten_ids: list[str]
    summary: str | None = None
    after_id: str | None = None       # summary goes right after this entry (None: at the start)
    reason: str = "soft"              # soft | hard | request
    method: str = "llm"               # llm | extractive | drop
    tokens_before: int = 0
    tokens_after: int = 0
    ts: float = Field(default_factory=system_clock)

    @property
    def summary_id(self) -> str:
        return f"{self.id}:summary"

    def summary_entry(self) -> Entry | None:
        if self.summary is None:
            return None
        return Entry(
            id=self.summary_id, kind=EntryKind.SUMMARY, ts=self.ts,
            content=self.summary, meta={"condensation": self.id, "method": self.method},
        )


class ContextEdit(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ED"))
    target_id: str
    replacement: str | None = None            # new content; None with omit=True drops the entry
    replacement_calls: list[ToolCall] | None = None
    omit: bool = False
    reason: str = "mask"
    ts: float = Field(default_factory=system_clock)


INCOMPLETE_TOOL_RESULT = "(this tool call did not complete; no result is available)"


def entry_tokens(entry: Entry, counter: TokenCounter) -> int:
    n = counter.count(entry.content) + MESSAGE_OVERHEAD_TOKENS
    for call in entry.tool_calls:
        n += counter.count(call.name) + counter.count(json.dumps(call.args, default=str))
    return n


def view_tokens(entries: Iterable[Entry], counter: TokenCounter) -> int:
    return sum(entry_tokens(e, counter) for e in entries)


def cut_points(view: Sequence[Entry]) -> list[int]:
    """Indices where a forgotten range may start or end without splitting a tool call
    from its result: any position whose entry is not a tool result, plus the end."""
    return [i for i, e in enumerate(view) if e.kind is not EntryKind.TOOL] + [len(view)]


class WorkingLog:
    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.condensations: list[Condensation] = []
        self.edits: dict[str, ContextEdit] = {}
        self._ids: set[str] = set()

    def __len__(self) -> int:
        return len(self.entries)

    def has(self, entry_id: str) -> bool:
        return entry_id in self._ids

    def append(self, entry: Entry) -> bool:
        if entry.id in self._ids:
            return False
        self.entries.append(entry)
        self._ids.add(entry.id)
        return True

    def add_condensation(self, condensation: Condensation) -> None:
        self.condensations.append(condensation)

    def add_edit(self, edit: ContextEdit) -> None:
        self.edits[edit.target_id] = edit

    def omit_turn(self, assistant_id: str, *, reason: str = "omit") -> list[ContextEdit]:
        """Hide an assistant turn and all of its tool results (e.g. an abandoned attempt)."""
        target = next((e for e in self.entries if e.id == assistant_id), None)
        if target is None:
            raise KeyError(assistant_id)
        call_ids = {c.id for c in target.tool_calls}
        ids = [assistant_id] + [
            e.id for e in self.entries if e.kind is EntryKind.TOOL and e.tool_call_id in call_ids
        ]
        edits = [ContextEdit(target_id=i, omit=True, reason=reason) for i in ids]
        for ed in edits:
            self.add_edit(ed)
        return edits

    def view(self) -> list[Entry]:
        out: list[Entry] = list(self.entries)
        for cond in self.condensations:
            forgotten = set(cond.forgotten_ids)
            idx = 0
            if cond.after_id is not None:
                for i, e in enumerate(out):
                    if e.id == cond.after_id:
                        idx = i + 1
                        break
            kept_before = [e for e in out[:idx] if e.id not in forgotten]
            kept_after = [e for e in out[idx:] if e.id not in forgotten]
            summary = cond.summary_entry()
            out = kept_before + ([summary] if summary else []) + kept_after
        edited: list[Entry] = []
        for e in out:
            ed = self.edits.get(e.id)
            if ed is None:
                edited.append(e)
            elif ed.omit:
                continue
            else:
                update: dict[str, Any] = {"meta": {**e.meta, "edited": ed.reason}}
                if ed.replacement is not None:
                    update["content"] = ed.replacement
                if ed.replacement_calls is not None:
                    update["tool_calls"] = ed.replacement_calls
                edited.append(e.model_copy(update=update))
        return repair(edited)


def repair(view: list[Entry]) -> list[Entry]:
    """Enforce call/result pairing: drop orphan results, stub missing ones."""
    out: list[Entry] = []
    pending: dict[str, str] = {}   # call id -> tool name, for the current assistant turn
    for e in view:
        if e.kind is EntryKind.TOOL:
            if e.tool_call_id in pending:
                pending.pop(e.tool_call_id)
                out.append(e)
            continue
        out.extend(_stub(pending))
        pending = {}
        out.append(e)
        if e.kind is EntryKind.ASSISTANT:
            pending = {c.id: c.name for c in e.tool_calls}
    out.extend(_stub(pending))
    return out


def _stub(pending: dict[str, str]) -> list[Entry]:
    return [
        Entry(id=f"stub:{cid}", kind=EntryKind.TOOL, tool_call_id=cid, name=name,
              content=INCOMPLETE_TOOL_RESULT, meta={"synthetic": True})
        for cid, name in pending.items()
    ]
