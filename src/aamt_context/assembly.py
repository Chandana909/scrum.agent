"""Budgeted context assembly.

A context is a list of :class:`Section` objects. The assembler allocates the token
budget greedily by priority (required sections first), each section capped by
``max_share`` of the budget, and lets each section shrink itself to its allocation —
lists drop their lowest-ranked items and say how many were left out, text is trimmed.
Output order is the declared ``order``, independent of priority, so layout is stable.

Every assembly produces a manifest: per-section tokens, which memory records were
shown, what was dropped, and a hash of the stable (static + project tier) part. The
manifest is what answers "why did the agent know / not know X?".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol

from ._util import stable_hash
from .tokens import TokenCounter, default_counter


class Tier(IntEnum):
    STATIC = 0     # role, skills: identical across tasks
    PROJECT = 1    # requirements, conventions, lessons: change per sprint
    TASK = 2       # this task's objective, decisions, upstream work, code
    VOLATILE = 3   # inbox, previous attempts: change every call


@dataclass
class Item:
    text: str
    record_id: str | None = None


@dataclass
class RenderedSection:
    key: str
    title: str
    text: str
    tokens: int
    tier: Tier
    record_ids: list[str] = field(default_factory=list)
    total_items: int = 0
    shown_items: int = 0
    truncated: bool = False


class Section(Protocol):
    key: str
    title: str
    tier: Tier
    priority: int
    order: int
    required: bool
    max_share: float | None

    def render(self, budget: int, counter: TokenCounter) -> RenderedSection | None: ...


def fit_text(text: str, max_tokens: int, counter: TokenCounter, *, keep: str = "head") -> str:
    """Longest prefix (or suffix) of ``text`` within ``max_tokens``."""
    if counter.count(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        piece = text[:mid] if keep == "head" else text[len(text) - mid:]
        if counter.count(piece) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    piece = text[:lo] if keep == "head" else text[len(text) - lo:]
    return (piece.rstrip() + " …") if keep == "head" else ("… " + piece.lstrip())


@dataclass
class TextSection:
    key: str
    title: str
    text: str
    tier: Tier = Tier.TASK
    priority: int = 50
    order: int = 50
    required: bool = False
    max_share: float | None = None
    keep: str = "head"
    record_ids: list[str] = field(default_factory=list)

    def render(self, budget: int, counter: TokenCounter) -> RenderedSection | None:
        if not self.text.strip():
            return None
        header = f"## {self.title}\n" if self.title else ""
        room = budget - counter.count(header)
        if room < 16:
            return None
        body = fit_text(self.text.strip(), room, counter, keep=self.keep)
        text = header + body
        return RenderedSection(self.key, self.title, text, counter.count(text), self.tier,
                               list(self.record_ids), 1, 1, body != self.text.strip())


@dataclass
class ListSection:
    key: str
    title: str
    items: list[Item]
    tier: Tier = Tier.TASK
    priority: int = 50
    order: int = 50
    required: bool = False
    max_share: float | None = None
    intro: str = ""
    more_hint: str = ""

    def render(self, budget: int, counter: TokenCounter) -> RenderedSection | None:
        if not self.items:
            return None
        head = (f"## {self.title}\n" if self.title else "") + (f"{self.intro}\n" if self.intro else "")
        used = counter.count(head)
        if used >= budget:
            return None
        lines: list[str] = []
        ids: list[str] = []
        footer_reserve = 16
        for item in self.items:
            cost = counter.count(item.text) + 1
            if used + cost > budget - footer_reserve:
                room = budget - footer_reserve - used - 1
                if not lines and room >= 24:          # at least show a trimmed first item
                    lines.append(fit_text(item.text, room, counter))
                    if item.record_id:
                        ids.append(item.record_id)
                break
            lines.append(item.text)
            used += cost
            if item.record_id:
                ids.append(item.record_id)
        if not lines:
            return None
        dropped = len(self.items) - len(lines)
        text = head + "\n".join(lines)
        if dropped:
            text += f"\n_(+{dropped} more{' — ' + self.more_hint if self.more_hint else ''})_"
        return RenderedSection(self.key, self.title, text, counter.count(text), self.tier, ids,
                               len(self.items), len(lines), dropped > 0)


@dataclass
class AssembledContext:
    text: str
    sections: list[RenderedSection]
    tokens: int
    budget: int
    dropped: list[str] = field(default_factory=list)   # had content, no budget left
    empty: list[str] = field(default_factory=list)     # nothing to show
    prefix_hash: str = ""

    @property
    def record_ids(self) -> list[str]:
        return [rid for s in self.sections for rid in s.record_ids]

    def section(self, key: str) -> RenderedSection | None:
        return next((s for s in self.sections if s.key == key), None)

    def manifest(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "budget": self.budget,
            "prefix_hash": self.prefix_hash,
            "dropped": list(self.dropped),
            "empty": list(self.empty),
            "sections": [
                {"key": s.key, "tier": int(s.tier), "tokens": s.tokens, "records": s.record_ids,
                 "shown": s.shown_items, "total": s.total_items, "truncated": s.truncated}
                for s in self.sections
            ],
        }


def _is_empty(section: Section) -> bool:
    items = getattr(section, "items", None)
    if items is not None:
        return not items
    text = getattr(section, "text", None)
    return text is not None and not str(text).strip()


class ContextAssembler:
    def __init__(self, counter: TokenCounter | None = None, *, separator: str = "\n\n"):
        self.counter = counter or default_counter()
        self.separator = separator

    def assemble(self, sections: Sequence[Section], budget: int) -> AssembledContext:
        declared = {s.key: i for i, s in enumerate(sections)}
        sep_cost = self.counter.count(self.separator)
        remaining = budget
        rendered: dict[str, RenderedSection] = {}
        dropped: list[str] = []
        empty = [s.key for s in sections if _is_empty(s)]
        for s in sorted(sections, key=lambda s: (not s.required, -s.priority, declared[s.key])):
            if s.key in empty:
                continue
            cap = remaining if s.max_share is None else min(remaining, int(budget * s.max_share))
            r = s.render(cap, self.counter) if cap > 0 else None
            if r is None:
                dropped.append(s.key)
                continue
            rendered[s.key] = r
            remaining -= r.tokens + sep_cost
        ordered = [
            rendered[s.key]
            for s in sorted(sections, key=lambda s: (s.order, declared[s.key]))
            if s.key in rendered
        ]
        text = self.separator.join(r.text for r in ordered)
        prefix = self.separator.join(r.text for r in ordered if r.tier <= Tier.PROJECT)
        return AssembledContext(
            text=text, sections=ordered, tokens=self.counter.count(text), budget=budget,
            dropped=dropped, empty=empty, prefix_hash=stable_hash(prefix) if prefix else "",
        )
