"""Retrospective learning: turn retro output into durable, de-duplicated lessons.

aamt currently keeps retro action items on the Sprint row and reuses them only through
a keyword check (``"reduce" and "scope"`` shrinks capacity). Here each retro item
becomes a LESSON (or ACTION_ITEM) record:

* lessons that recur across sprints merge into one record whose ``support`` grows —
  the "recurring blocker" signal the PRD asks for (§22) falls out of de-duplication;
* a lesson that clearly concerns one discipline is scoped to that team, so the
  frontend agent is not handed backend advice;
* action items stay open until resolved, and planning can list the open ones.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .llm import TextLLM, complete_json
from .memory import SharedMemory
from .types import MemoryKind, MemoryRecord, MemoryStatus, Scope, Visibility, WriteResult

DEFAULT_TEAM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "backend": ("backend", "endpoint", "server", "api handler"),
    "frontend": ("frontend", "ui", "react", "css", "component"),
    "database": ("database", "schema", "sql", "migration", "orm"),
    "qa": ("qa", "test suite", "tests", "testing", "pytest", "coverage"),
    "devops": ("devops", "ci", "docker", "deploy", "pipeline"),
    "ml": ("ml", "model", "inference", "training"),
}

_REWRITE_SYSTEM = """You turn sprint retrospective notes into reusable engineering lessons.
Reply with ONLY JSON: {"lessons": [{"lesson": str, "polarity": "negative"|"positive"|"change",
"team": str|null}]}. Each lesson is ONE imperative sentence that would help a future
sprint (e.g. "Freeze API contracts before frontend work starts"). Keep project-specific
nouns. Set team only when the lesson clearly concerns one discipline
(backend, frontend, database, qa, devops, ml). Drop items with no reusable lesson."""


class LessonConsolidator:
    def __init__(
        self,
        memory: SharedMemory,
        *,
        llm: TextLLM | None = None,
        author: str = "scrum-master",
        team_keywords: dict[str, tuple[str, ...]] | None = None,
    ):
        self.memory = memory
        self.llm = llm
        self.author = author
        self.team_keywords = team_keywords or DEFAULT_TEAM_KEYWORDS

    def team_for(self, text: str) -> str | None:
        low = f" {text.lower()} "
        hits = {
            team: sum(1 for kw in kws if f" {kw} " in low or f" {kw}s " in low or f" {kw}," in low)
            for team, kws in self.team_keywords.items()
        }
        best = [t for t, n in hits.items() if n and n == max(hits.values())]
        return best[0] if len(best) == 1 else None

    def ingest_retrospective(
        self,
        project_id: str,
        *,
        sprint_id: str | None = None,
        went_well: Sequence[str] = (),
        went_poorly: Sequence[str] = (),
        changes: Sequence[str] = (),
        action_items: Sequence[str] = (),
        source_prefix: str | None = None,
    ) -> list[WriteResult]:
        items = self._lessons(went_well, went_poorly, changes)
        results: list[WriteResult] = []
        for i, (polarity, text, team) in enumerate(items):
            scope = Scope.team(project_id, team) if team else Scope.project(project_id)
            results.append(self.memory.remember(
                scope=scope, kind=MemoryKind.LESSON, author=self.author, title=text,
                importance=0.45 if polarity == "positive" else 0.7,
                tags=["retro", polarity], sprint_id=sprint_id,
                data={"polarity": polarity, "sprint": sprint_id},
                source_key=f"{source_prefix}:lesson:{i}" if source_prefix else None,
            ))
        for j, action in enumerate(a for a in action_items if a.strip()):
            results.append(self.memory.remember(
                scope=Scope.project(project_id), kind=MemoryKind.ACTION_ITEM, author=self.author,
                title=action.strip(), tags=["retro"], sprint_id=sprint_id,
                data={"sprint": sprint_id},
                source_key=f"{source_prefix}:action:{j}" if source_prefix else None,
            ))
        return results

    def _lessons(
        self, went_well: Sequence[str], went_poorly: Sequence[str], changes: Sequence[str]
    ) -> list[tuple[str, str, str | None]]:
        raw = (
            [("negative", t) for t in went_poorly]
            + [("change", t) for t in changes]
            + [("positive", t) for t in went_well]
        )
        raw = [(p, t.strip()) for p, t in raw if t and t.strip()]
        if not raw:
            return []
        if self.llm is not None:
            listing = "\n".join(f"- [{p}] {t}" for p, t in raw)
            data = complete_json(self.llm, _REWRITE_SYSTEM, listing, max_tokens=900)
            rewritten: list[tuple[str, str, str | None]] = []
            for item in (data or {}).get("lessons", []) or []:
                if not isinstance(item, dict) or not str(item.get("lesson", "")).strip():
                    continue
                raw_polarity = str(item.get("polarity") or "")
                polarity = raw_polarity if raw_polarity in ("negative", "positive", "change") else "change"
                raw_team = item.get("team")
                team = raw_team if isinstance(raw_team, str) and raw_team in self.team_keywords else None
                rewritten.append((polarity, str(item["lesson"]).strip(), team))
            if rewritten:
                return rewritten
        return [(p, t, self.team_for(t)) for p, t in raw]

    def open_action_items(self, project_id: str) -> list[MemoryRecord]:
        return self.memory.store.query(
            scopes=[Scope.project(project_id)], kinds=[MemoryKind.ACTION_ITEM],
            statuses=[MemoryStatus.ACTIVE], order="created",
        )

    def lessons_for(
        self, visibility: Visibility, query: str = "", *, limit: int = 8, polarities: Iterable[str] | None = None,
    ) -> list[MemoryRecord]:
        hits = self.memory.recall(query, visibility, kinds=[MemoryKind.LESSON], limit=limit * 2)
        wanted = set(polarities) if polarities else None
        out = [h.record for h in hits if wanted is None or h.record.data.get("polarity") in wanted]
        return out[:limit]
