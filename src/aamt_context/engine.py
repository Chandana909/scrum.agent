"""ContextEngine: the composition root and the one object a host needs.

    engine = ContextEngine.open(".aamt/context.db", llm=my_llm)
    brief  = engine.build_brief(TaskBrief(...), ReaderContext(project_id=..., task_id=..., ...))
    sess   = engine.session("T-1#a1")          # working context for one agent run
    engine.record_attempt(...), engine.record_handoff(...)
    engine.memory / engine.channels / engine.meetings / engine.lessons

The brief is assembled from shared memory every time (durable knowledge lives outside
transcripts and is re-injected, the way Claude Code re-reads CLAUDE.md and memory after
compaction), so compaction of a run never has to preserve project knowledge.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._util import Clock, first_line, new_id
from .assembly import AssembledContext, ContextAssembler, Item, ListSection, Section, TextSection, Tier
from .attempts import AttemptSummary
from .briefs import HandoffReport, TaskBrief
from .channels import ChannelHub, Channels, render_message
from .codemap import CodeMap
from .condensers import CondenserPipeline, default_pipeline
from .config import BudgetConfig, ContextConfig
from .lessons import LessonConsolidator
from .llm import TextLLM
from .meetings import MeetingRoom, MinutesExtractor
from .memory import Embedder, SharedMemory, WritePolicy
from .session import ContextSession
from .store import SqliteMemoryStore
from .tokens import TokenCounter, default_counter
from .types import MemoryKind, MemoryRecord, MemoryStatus, Scope, Visibility, WriteResult


@dataclass
class ReaderContext:
    """Who is about to read a context: decides visibility, inbox and code root."""

    project_id: str
    agent_id: str | None = None
    role: str | None = None
    team: str | None = None
    task_id: str | None = None
    task_scope: str | None = None       # explicit path for sub-tasks (Scope.task(..., parent=...))
    sprint_id: str | None = None
    aliases: tuple[str, ...] = ()
    code_root: str | None = None

    @property
    def scope(self) -> str | None:
        if self.task_scope:
            return self.task_scope
        return Scope.task(self.project_id, self.task_id) if self.task_id else None

    def visibility(self, *, weights: dict[str, float] | None = None) -> Visibility:
        return Visibility.build(self.project_id, task_scope=self.scope, team=self.team,
                                agent_id=self.agent_id, weights=weights)

    def channels(self) -> list[str]:
        if self.agent_id:
            return ChannelHub.subscriptions(project_id=self.project_id, agent_id=self.agent_id, team=self.team)
        chans = [Channels.project(self.project_id)]
        return chans + ([Channels.team(self.project_id, self.team)] if self.team else [])

    def reader_aliases(self) -> list[str]:
        return [a for a in (*self.aliases, self.role, self.team) if a]


def _decision_line(r: MemoryRecord) -> str:
    line = f"- [{r.id}] {r.kind.value}: {r.title}"
    rationale = r.data.get("rationale")
    if rationale:
        line += f" — because {rationale}"
    if r.data.get("open_challenges"):
        line += " (challenged; under review)"
    return line


def _clarification_line(r: MemoryRecord) -> str:
    q = r.data.get("question") or r.title
    a = r.data.get("answer")
    return f"- Q: {q}\n  A: {a}" if a else f"- OPEN QUESTION: {q}"


class ContextEngine:
    def __init__(
        self,
        store: SqliteMemoryStore,
        *,
        config: ContextConfig | None = None,
        llm: TextLLM | None = None,
        counter: TokenCounter | None = None,
        embedder: Embedder | None = None,
        policy: WritePolicy | None = None,
        clock: Clock | None = None,
    ):
        self.store = store
        self.config = config or ContextConfig()
        self.llm = llm
        self.counter = counter or default_counter()
        self.clock = clock or store.clock
        self.memory = SharedMemory(store, config=self.config, policy=policy, embedder=embedder, clock=self.clock)
        self.channels = ChannelHub(store, clock=self.clock)
        self.meetings = MeetingRoom(self.memory, self.channels, extractor=MinutesExtractor(llm),
                                    counter=self.counter, clock=self.clock)
        self.lessons = LessonConsolidator(self.memory, llm=llm)
        self.assembler = ContextAssembler(self.counter)
        self._codemaps: dict[str, CodeMap] = {}

    @classmethod
    def open(cls, path: str | Path, **kw: Any) -> ContextEngine:
        clock = kw.get("clock")
        store = SqliteMemoryStore(path, clock=clock) if clock else SqliteMemoryStore(path)
        return cls(store, **kw)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------ working context
    def session(
        self, session_id: str, *, budget: BudgetConfig | None = None, goal: str | None = None,
        pipeline: CondenserPipeline | None = None,
    ) -> ContextSession:
        return ContextSession(
            session_id, budget=budget or self.config.budget, counter=self.counter, llm=self.llm,
            store=self.store, pipeline=pipeline or default_pipeline(), clock=self.clock, goal=goal,
        )

    # --------------------------------------------------------------- briefs
    def codemap(self, root: str) -> CodeMap:
        cm = self._codemaps.get(root)
        if cm is None:
            cm = self._codemaps[root] = CodeMap(root)
        return cm

    def latest_handoff(self, task_id: str) -> MemoryRecord | None:
        hits = self.store.query(kinds=[MemoryKind.HANDOFF], subject=f"handoff:{task_id}", limit=1)
        return hits[0] if hits else None

    def build_brief(
        self,
        brief: TaskBrief,
        reader: ReaderContext,
        *,
        budget_tokens: int | None = None,
        extra_sections: Sequence[Section] = (),
        include: Iterable[str] | None = None,
        include_task_core: bool = True,
        memory_index: bool = False,
        ack_inbox: bool = True,
        persist_manifest: bool = True,
    ) -> AssembledContext:
        """Assemble the context for one task.

        ``include_task_core=False`` returns only the supporting context, for hosts that
        render the task themselves (aamt's ``render_task_context``).
        """
        budget = budget_tokens or self.config.brief_tokens
        vis = reader.visibility()
        query = brief.retrieval_query()
        ents = brief.retrieval_entities()
        shown: set[str] = set()
        sections: list[Section] = []
        if include_task_core:
            sections.append(TextSection(
                "task", "", brief.render_core(), tier=Tier.TASK, priority=100, order=0,
                required=True, max_share=0.35,
            ))

        # this task's own history: failed attempts, review/integration feedback, blockers
        if reader.scope:
            attempts = self.store.query(scopes=[reader.scope], kinds=[MemoryKind.ATTEMPT], order="created")[-3:]
            feedback = self.store.query(scopes=[reader.scope], kinds=[MemoryKind.FACT], order="created",
                                        tags_any=["review", "integration", "verification"])[-4:]
            blockers = self.store.query(scopes=[reader.scope], kinds=[MemoryKind.BLOCKER])
            hist = [Item(r.body or r.title, r.id) for r in (*attempts, *feedback)]
            hist += [Item(f"- BLOCKER: {r.title}", r.id) for r in blockers]
            shown.update(r.id for r in (*attempts, *feedback, *blockers))
            sections.append(ListSection(
                "history", "Previous attempts and feedback on this task", hist, tier=Tier.VOLATILE,
                priority=95, order=10, max_share=0.25,
                intro="Build on what worked; do not repeat what failed.",
            ))

        # upstream handoffs
        upstream: list[Item] = []
        upstream_files: list[str] = []
        for dep in brief.dependencies:
            h = self.latest_handoff(dep)
            if h is None:
                upstream.append(Item(f"- {dep}: no handoff recorded yet — inspect the repository"))
                continue
            upstream.append(Item(h.body or h.title, h.id))
            upstream_files += list(h.data.get("files_changed", []))
            shown.add(h.id)
        sections.append(ListSection("upstream", "Upstream work you build on", upstream, tier=Tier.TASK,
                                    priority=90, order=20, max_share=0.2))

        # decisions and contracts in force: the relevant ones first, then every other active one
        # by importance (truncated by budget) — agents must not miss decisions made elsewhere
        dec_kinds = [MemoryKind.DECISION, MemoryKind.CONTRACT, MemoryKind.ASSUMPTION]
        dec = self.memory.recall(query, vis, kinds=dec_kinds, entities=ents, limit=12,
                                 sprint_id=reader.sprint_id, exclude_ids=shown)
        shown.update(s.record.id for s in dec)
        rest = [r for r in self.store.query(scopes=vis.scope_list, kinds=dec_kinds, order="importance", limit=40)
                if r.id not in shown]
        shown.update(r.id for r in rest)
        sections.append(ListSection(
            "decisions", "Decisions and contracts in force",
            [Item(_decision_line(s.record), s.record.id) for s in dec] + [Item(_decision_line(r), r.id) for r in rest],
            tier=Tier.TASK, priority=85, order=30, max_share=0.2,
            more_hint="use memory_search for more" if memory_index else "",
        ))

        # requirements and standards: pinned first, then relevant
        pinned = self.memory.pinned(vis, kinds=[MemoryKind.REQUIREMENT, MemoryKind.CONVENTION])
        req_hits = self.memory.recall(query, vis, kinds=[MemoryKind.REQUIREMENT, MemoryKind.CONVENTION],
                                      entities=ents, limit=8, exclude_ids={*shown, *(p.id for p in pinned)})
        reqs = [*pinned, *(s.record for s in req_hits)]
        shown.update(r.id for r in reqs)
        sections.append(ListSection(
            "requirements", "Project requirements and standards",
            [Item(f"- {r.title}" + (f": {first_line(r.body, 300)}" if r.body and r.body != r.title else ""), r.id)
             for r in reqs],
            tier=Tier.PROJECT, priority=80, order=40, max_share=0.15,
        ))

        clar = self.memory.recall(query, vis, kinds=[MemoryKind.CLARIFICATION], entities=ents, limit=6, exclude_ids=shown)
        shown.update(s.record.id for s in clar)
        sections.append(ListSection(
            "clarifications", "Clarifications (already answered — don't re-ask)",
            [Item(_clarification_line(s.record), s.record.id) for s in clar],
            tier=Tier.TASK, priority=70, order=50, max_share=0.1,
        ))

        les = self.memory.recall(query, vis, kinds=[MemoryKind.LESSON], entities=ents, limit=6, exclude_ids=shown)
        shown.update(s.record.id for s in les)
        sections.append(ListSection(
            "lessons", "Lessons from earlier sprints",
            [Item(f"- {s.record.title}" + (f" (seen {s.record.support}x)" if s.record.support > 1 else ""), s.record.id)
             for s in les],
            tier=Tier.PROJECT, priority=65, order=60, max_share=0.1,
        ))

        if reader.code_root:
            code_text, _paths = self.codemap(reader.code_root).render(
                query, int(budget * 0.2), counter=self.counter, hints=[*ents, *upstream_files],
            )
            sections.append(TextSection("code", "Relevant code (outlines)", code_text, tier=Tier.TASK,
                                        priority=60, order=70, max_share=0.2))

        risks = self.memory.recall(query, vis, kinds=[MemoryKind.BLOCKER, MemoryKind.RISK], entities=ents,
                                   limit=5, exclude_ids=shown)
        shown.update(s.record.id for s in risks)
        sections.append(ListSection(
            "risks", "Open blockers and risks elsewhere", [Item(f"- {s.record.kind.value}: {s.record.title}", s.record.id)
                                                            for s in risks],
            tier=Tier.TASK, priority=45, order=80, max_share=0.08,
        ))

        inbox = []
        if reader.agent_id:
            inbox = self.channels.unread(reader.agent_id, reader.channels(), aliases=reader.reader_aliases())
            sections.append(ListSection(
                "inbox", "Messages for you", [Item(render_message(m), m.id) for m in inbox],
                tier=Tier.VOLATILE, priority=75, order=90, max_share=0.12,
            ))

        if memory_index:
            more = self.memory.recall(query, vis, entities=ents, limit=25, exclude_ids=shown)
            sections.append(ListSection(
                "index", "More in shared memory (open with memory_read)",
                [Item(f"- {s.record.id} {s.record.kind.value}: {first_line(s.record.title, 120)}", s.record.id)
                 for s in more],
                tier=Tier.TASK, priority=20, order=100, max_share=0.06,
            ))

        sections += list(extra_sections)
        if include is not None:
            keep = {*include, "task"}
            sections = [s for s in sections if s.key in keep]

        assembled = self.assembler.assemble(sections, budget)
        record_ids = [i for i in assembled.record_ids if i.startswith("MR-")]
        self.store.touch(record_ids)
        if ack_inbox and inbox:
            shown_msgs = set(assembled.section("inbox").record_ids) if assembled.section("inbox") else set()
            self.channels.ack(reader.agent_id, [m for m in inbox if m.id in shown_msgs])
        if persist_manifest:
            self.store.append_ctx(
                f"brief:{reader.task_id or reader.agent_id or 'adhoc'}", "manifest", new_id("MF"),
                {**assembled.manifest(), "reader": reader.__dict__, "query": query[:500], "entities": ents},
            )
        return assembled

    # ------------------------------------------------- handoffs & attempts
    def record_attempt(
        self, summary: AttemptSummary, reader: ReaderContext, *, source_key: str | None = None,
    ) -> WriteResult:
        if not reader.scope:
            raise ValueError("record_attempt needs a task scope (ReaderContext.task_id/task_scope)")
        # attempt numbers restart whenever a task is re-run, so they are not identities:
        # callers that need idempotency pass a key that includes their run id
        return self.memory.remember(
            scope=reader.scope, kind=MemoryKind.ATTEMPT, author=reader.agent_id or "system", role=reader.role,
            title=summary.title(), body=summary.render(), data=summary.model_dump(),
            entities=[x for x in (reader.task_id, *summary.files_modified, *summary.failing_tests) if x],
            sprint_id=reader.sprint_id, source_key=source_key,
        )

    def attempts(self, reader: ReaderContext) -> list[MemoryRecord]:
        if not reader.scope:
            return []
        return self.store.query(scopes=[reader.scope], kinds=[MemoryKind.ATTEMPT], order="created")

    def record_handoff(
        self, report: HandoffReport, reader: ReaderContext, *, source_key: str | None = None,
        announce: bool = True,
    ) -> list[WriteResult]:
        """Persist a child's report; its decisions/interfaces become proposals for the lead."""
        scope = reader.scope or Scope.task(reader.project_id, report.task_id)
        shared = Scope.team(reader.project_id, reader.team) if reader.team else Scope.project(reader.project_id)
        project = Scope.project(reader.project_id)
        author, role, sprint = report.agent, reader.role, reader.sprint_id
        entities = [report.task_id, *report.files_changed]
        results = [self.memory.remember(
            scope=scope, kind=MemoryKind.HANDOFF, author=author, role=role,
            subject=f"handoff:{report.task_id}", title=f"{report.task_id}: {first_line(report.summary or report.outcome, 140)}",
            body=report.render(), data=report.model_dump(), entities=entities, sprint_id=sprint,
            source_key=source_key,
        )]
        common = dict(author=author, role=role, entities=entities, sprint_id=sprint,
                      data={"from_handoff": report.task_id})
        for d in report.decisions:
            results.append(self.memory.remember(scope=shared, kind=MemoryKind.DECISION, title=d, **common))
        for c in report.interfaces:
            results.append(self.memory.remember(scope=project, kind=MemoryKind.CONTRACT, title=c, **common))
            if announce:
                self.channels.post(Channels.project(reader.project_id), author, c, type="API_CONTRACT_UPDATE",
                                   related=[report.task_id])
        for a in report.assumptions:
            results.append(self.memory.remember(scope=shared, kind=MemoryKind.ASSUMPTION, title=a, **common))
        for q in report.open_questions:
            results.append(self.memory.remember(
                scope=shared, kind=MemoryKind.CLARIFICATION, title=q, tags=["open-question"],
                **{**common, "data": {"question": q, "open": True, "from_handoff": report.task_id}},
            ))
        for f in report.follow_ups:
            results.append(self.memory.remember(scope=project, kind=MemoryKind.ACTION_ITEM, title=f,
                                                tags=["follow-up"], **common))
        for r in report.risks:
            results.append(self.memory.remember(scope=shared, kind=MemoryKind.RISK, title=r, **common))
        return results

    def proposals(self, visibility: Visibility) -> list[MemoryRecord]:
        """Everything awaiting a curator (Scrum Master / human) decision."""
        return self.store.query(scopes=visibility.scope_list, statuses=[MemoryStatus.PROPOSED], order="created")
