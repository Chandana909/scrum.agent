"""Core data model: scopes, visibility, and typed memory records.

A record lives in exactly one *scope* (a path such as ``p/P-1/team/backend``). What an
agent can see is a :class:`Visibility`: the union of the scope chains of its task, team
and agent, plus ``org``. Scopes decide access; kinds, entities and text decide relevance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ._util import content_hash, new_id, normalize_entity, system_clock


# ---------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------
def _seg(value: str) -> str:
    return str(value).strip().replace("/", "_") or "_"


class Scope:
    """Helpers for scope paths. Scopes are plain strings so they are cheap to store/query."""

    ORG = "org"

    @staticmethod
    def project(project_id: str) -> str:
        return f"p/{_seg(project_id)}"

    @staticmethod
    def team(project_id: str, team: str) -> str:
        return f"{Scope.project(project_id)}/team/{_seg(team)}"

    @staticmethod
    def agent(project_id: str, agent_id: str) -> str:
        return f"{Scope.project(project_id)}/agent/{_seg(agent_id)}"

    @staticmethod
    def task(project_id: str, task_id: str, *, parent: str | None = None) -> str:
        """Task scope; pass the parent task's scope for recursive sub-tasks."""
        base = parent or Scope.project(project_id)
        return f"{base}/task/{_seg(task_id)}"

    @staticmethod
    def meeting(project_id: str, meeting_id: str) -> str:
        return f"{Scope.project(project_id)}/meeting/{_seg(meeting_id)}"

    @staticmethod
    def chain(scope: str) -> list[str]:
        """``p/P/task/A/task/B`` -> ``[p/P, p/P/task/A, p/P/task/A/task/B]``."""
        if scope == Scope.ORG:
            return [Scope.ORG]
        parts = scope.split("/")
        return ["/".join(parts[:i]) for i in range(2, len(parts) + 1, 2)]

    @staticmethod
    def level(scope: str) -> str:
        if scope == Scope.ORG:
            return "org"
        parts = scope.split("/")
        return "project" if len(parts) == 2 else parts[-2]

    @staticmethod
    def project_of(scope: str) -> str | None:
        parts = scope.split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "p" else None

    @staticmethod
    def leaf_id(scope: str) -> str:
        return scope.split("/")[-1]


DEFAULT_SCOPE_WEIGHTS: dict[str, float] = {
    "org": 0.4,
    "project": 0.6,
    "meeting": 0.7,
    "team": 0.8,
    "agent": 0.85,
    "task": 1.0,
}


class Visibility(BaseModel):
    """The set of scopes a reader may see, each with a relevance weight (0..1)."""

    scopes: dict[str, float]

    @classmethod
    def build(
        cls,
        project_id: str,
        *,
        task_scope: str | None = None,
        team: str | None = None,
        agent_id: str | None = None,
        include_org: bool = True,
        extra: tuple[str, ...] | list[str] = (),
        weights: dict[str, float] | None = None,
    ) -> Visibility:
        w = {**DEFAULT_SCOPE_WEIGHTS, **(weights or {})}
        scopes: dict[str, float] = {}
        if include_org:
            scopes[Scope.ORG] = w["org"]
        scopes[Scope.project(project_id)] = w["project"]
        if task_scope:
            task_chain = [s for s in Scope.chain(task_scope) if Scope.level(s) == "task"]
            # nearest task is most relevant; each ancestor task a little less
            for depth, s in enumerate(reversed(task_chain)):
                scopes[s] = max(w["task"] - 0.1 * depth, w["team"])
        if team:
            scopes[Scope.team(project_id, team)] = w["team"]
        if agent_id:
            scopes[Scope.agent(project_id, agent_id)] = w["agent"]
        for s in extra:
            scopes.setdefault(s, w.get(Scope.level(s), w["project"]))
        return cls(scopes=scopes)

    @property
    def scope_list(self) -> list[str]:
        return sorted(self.scopes)

    def includes(self, scope: str) -> bool:
        return scope in self.scopes

    def weight(self, scope: str) -> float:
        return self.scopes.get(scope, 0.0)


# ---------------------------------------------------------------------------
# memory records
# ---------------------------------------------------------------------------
class MemoryKind(str, Enum):
    REQUIREMENT = "requirement"      # PRD items, acceptance criteria, constraints
    CLARIFICATION = "clarification"  # question/answer pairs; open questions; challenges
    ASSUMPTION = "assumption"
    DECISION = "decision"            # ADR-style: decision + rationale + alternatives
    CONTRACT = "contract"            # interfaces between agents (API shapes, schemas)
    CONVENTION = "convention"        # coding standards, repo rules, constraints
    LESSON = "lesson"                # retrospective / post-mortem learnings
    FACT = "fact"                    # discovered technical facts, review feedback
    BLOCKER = "blocker"
    RISK = "risk"
    ACTION_ITEM = "action_item"
    HANDOFF = "handoff"              # what a finished task delivered, for dependents
    ATTEMPT = "attempt"              # what a failed attempt tried and why it failed
    NOTE = "note"                    # agent-private scratch notes
    SUMMARY = "summary"              # meeting minutes, standup/sprint roll-ups
    ARTIFACT = "artifact"            # pointer + digest of a document (PRD.md, design.md)


class MemoryStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class Trust(str, Enum):
    """Where content came from. Tool-derived content is data, never instructions."""

    HUMAN = "human"
    CURATED = "curated"
    AGENT = "agent"
    TOOL = "tool"

    @property
    def rank(self) -> int:
        return {"human": 3, "curated": 2, "agent": 1, "tool": 0}[self.value]


@dataclass(frozen=True)
class KindPolicy:
    importance: float
    half_life_hours: float | None  # None: no recency decay
    near_dup: bool                 # merge near-duplicate writes instead of adding
    subject_keyed: bool            # at most one active record per (scope chain, subject)
    curated: bool                  # shared-scope writes by non-curators become proposals


KIND_POLICIES: dict[MemoryKind, KindPolicy] = {
    MemoryKind.REQUIREMENT: KindPolicy(0.9, None, False, True, True),
    MemoryKind.CLARIFICATION: KindPolicy(0.7, None, True, False, False),
    MemoryKind.ASSUMPTION: KindPolicy(0.6, 24 * 30, False, True, True),
    MemoryKind.DECISION: KindPolicy(0.85, None, False, True, True),
    MemoryKind.CONTRACT: KindPolicy(0.9, None, False, True, True),
    MemoryKind.CONVENTION: KindPolicy(0.8, None, True, True, True),
    MemoryKind.LESSON: KindPolicy(0.65, 24 * 60, True, False, False),
    MemoryKind.FACT: KindPolicy(0.5, 24 * 14, True, True, False),
    MemoryKind.BLOCKER: KindPolicy(0.75, 24 * 3, True, False, False),
    MemoryKind.RISK: KindPolicy(0.6, 24 * 14, True, False, False),
    MemoryKind.ACTION_ITEM: KindPolicy(0.6, 24 * 21, True, False, False),
    MemoryKind.HANDOFF: KindPolicy(0.8, None, False, True, False),
    MemoryKind.ATTEMPT: KindPolicy(0.7, 24 * 2, False, False, False),
    MemoryKind.NOTE: KindPolicy(0.4, 24 * 7, True, False, False),
    MemoryKind.SUMMARY: KindPolicy(0.5, 24 * 7, False, True, False),
    MemoryKind.ARTIFACT: KindPolicy(0.85, None, False, True, True),
}


def kind_policy(kind: MemoryKind | str) -> KindPolicy:
    return KIND_POLICIES[MemoryKind(kind)]


class Provenance(BaseModel):
    author: str                                        # agent id, "scrum-master", "human", "system"
    trust: Trust = Trust.AGENT
    source_events: list[str] = Field(default_factory=list)  # host event ids (aamt E-...)
    source_ref: str | None = None                      # meeting id, task id, file path, ...


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("MR"))
    scope: str
    kind: MemoryKind
    status: MemoryStatus = MemoryStatus.ACTIVE
    subject: str | None = None          # canonical key for supersession, e.g. "api.transport"
    title: str                          # one line; shown in indexes
    body: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)  # task ids, file paths, components
    importance: float = 0.5
    confidence: float = 1.0
    pinned: bool = False                # always injected for readers who can see it
    support: int = 1                    # how many independent writes said this
    provenance: Provenance
    source_key: str | None = None       # idempotency key for ingestion
    sprint_id: str | None = None
    content_hash: str = ""
    created_at: float = Field(default_factory=system_clock)
    updated_at: float = Field(default_factory=system_clock)
    valid_from: float = Field(default_factory=system_clock)
    valid_to: float | None = None
    superseded_by: str | None = None
    version: int = 1
    access_count: int = 0
    last_accessed_at: float | None = None

    def model_post_init(self, context: Any, /) -> None:
        self.entities = sorted({normalize_entity(e) for e in self.entities if e})
        self.tags = sorted({t.strip().lower() for t in self.tags if t and t.strip()})
        if not self.content_hash:
            self.content_hash = content_hash(self.kind.value, self.title, self.body)

    @property
    def author(self) -> str:
        return self.provenance.author

    @property
    def is_live(self) -> bool:
        return self.status in (MemoryStatus.ACTIVE, MemoryStatus.PROPOSED)


class WriteAction(str, Enum):
    ADDED = "added"
    PROPOSED = "proposed"
    DUPLICATE = "duplicate"
    MERGED = "merged"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"


@dataclass
class WriteResult:
    action: WriteAction
    record: MemoryRecord
    related: list[str] = field(default_factory=list)  # superseded / conflicting / merged-into ids


@dataclass
class ScoredRecord:
    record: MemoryRecord
    score: float
    components: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# channel messages (communication memory)
# ---------------------------------------------------------------------------
BROADCAST = "*"


class ChannelMessage(BaseModel):
    seq: int = 0                         # assigned by the store; total order per database
    id: str = Field(default_factory=lambda: new_id("CM"))
    channel: str
    sender: str
    recipients: list[str] = Field(default_factory=lambda: [BROADCAST])
    type: str = "MESSAGE"                # API_CONTRACT_UPDATE, QUESTION, UTTERANCE, ...
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    related: list[str] = Field(default_factory=list)  # task ids etc.
    reply_to: str | None = None
    ts: float = Field(default_factory=system_clock)

    def addressed_to(self, aliases: set[str]) -> bool:
        return BROADCAST in self.recipients or bool(aliases.intersection(self.recipients))
