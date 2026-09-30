"""aamt integration.

Four pieces, each usable on its own:

* :class:`AamtContextBridge.ingest_event` — subscribe to aamt's ``EventBus``; events become
  shared memory (decisions, blockers, handoffs, review feedback, merge conflicts, sprint
  goals, standups, retro lessons). No emitter has to change; ingestion is idempotent.
* :meth:`AamtContextBridge.brief_for_task` — the supporting context for one aamt task,
  to pass where the orchestrator currently passes ``_project_summary(project)``.
* :class:`AamtTaskContext` — per-task hooks for the developer agent: clip + restore tool
  output, condense the ReAct transcript before each model call, and remember failed
  attempts across retries.
* :class:`ChannelMailbox` — ``Mailbox``-compatible messaging with per-reader cursors.

Everything is duck-typed against aamt's models, so importing this module does not import
aamt (aamt's events, tasks and stores are only touched through their attributes).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .._util import first_line, new_id
from ..attempts import AttemptSummary, summarize_attempt
from ..briefs import HandoffReport, TaskBrief
from ..channels import Channels
from ..engine import ContextEngine, ReaderContext
from ..session import ContextSession
from ..types import ChannelMessage, MemoryKind, MemoryStatus, Scope

logger = logging.getLogger(__name__)

_TASK_ID = re.compile(r"\bT-[a-z0-9]{3,}\b")
OPERATIONAL_CONTEXTS = ("reporting", "resume")


def _etype(event: Any) -> str:
    return str(getattr(event.type, "value", event.type))


class AamtContextBridge:
    def __init__(
        self,
        engine: ContextEngine,
        *,
        curator: str = "scrum-master",
        memory_tools: bool = False,
        code_outline: bool = True,
        brief_tokens: int | None = None,
        store: Any = None,
        attempts_from_events: bool = True,
    ):
        self.engine = engine
        self.curator = curator
        self.memory_tools = memory_tools
        self.code_outline = code_outline
        self.brief_tokens = brief_tokens
        self.store = store                      # aamt ProjectStore (optional but recommended)
        # When the task graph passes an AamtTaskContext it records rich attempts itself;
        # set False then, so AGENT_FAILED / failed TASK_VERIFIED events don't add thin duplicates.
        self.attempts_from_events = attempts_from_events
        self.errors: list[tuple[str, str]] = []   # recent ingestion failures (last 1000)
        self._project_id: str | None = None
        self._synced_project = False

    @property
    def memory(self):
        return self.engine.memory

    # ------------------------------------------------------------------ wiring
    def attach(self, bus: Any, *, store: Any = None) -> Callable[[], None]:
        if store is not None:
            self.store = store
        return bus.subscribe(self.ingest_event)

    def ingest_event(self, event: Any) -> bool:
        """Turn one aamt event into memory. Returns True if it was handled now.

        The event is marked processed only after its handler succeeds, so a failure (a
        locked database, a bug) is retried when the event is delivered again. Handlers
        are idempotent: their writes carry ``source_key``s derived from the event id.
        """
        handler = getattr(self, f"_on_{_etype(event).lower()}", None)
        key = f"event:{event.id}"
        if handler is None or self.engine.store.is_claimed(key):
            return False
        try:
            handler(event, dict(getattr(event, "payload", None) or {}))
        except Exception as exc:  # never break the producer; the event stays retryable
            logger.exception("aamt event %s (%s) could not be ingested", event.id, _etype(event))
            self.errors.append((event.id, f"{type(exc).__name__}: {exc}"))
            del self.errors[:-1_000]
            return False
        return self.engine.store.claim(key)

    # --------------------------------------------------------------- helpers
    def _pid(self, event: Any = None) -> str | None:
        pid = getattr(event, "project_id", None) or self._project_id
        if pid is None and self.store is not None:
            project = self.store.get_project()
            pid = project.id if project else None
        if pid:
            self._project_id = pid
        return pid

    def _assignee(self, task_id: str | None) -> tuple[str, str | None]:
        """(agent id, role) of the task's assignee, else ("system", task role)."""
        if not task_id or self.store is None:
            return "system", None
        task = self.store.get_task(task_id)
        if task is None:
            return "system", None
        agent = self.store.get_agent(task.assignee) if task.assignee else None
        return (agent.id, agent.role) if agent else ("system", task.role)

    def _repo_files(self, commit: str | None) -> list[str]:
        if not commit or self.store is None:
            return []
        project = self.store.get_project()
        repo = getattr(project, "repo_path", None)
        if not repo:
            return []
        try:
            res = subprocess.run(
                ["git", "-C", repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()] if res.returncode == 0 else []

    # --------------------------------------------------------- store sync
    def sync_from_store(self) -> None:
        """Idempotently import what aamt already persisted (resume, late attach)."""
        self.sync_project()
        self.sync_decisions()
        self.sync_retrospectives()

    def sync_project(self) -> None:
        if self.store is None:
            return
        project = self.store.get_project()
        if project is None:
            return
        pid = self._pid()
        scope = Scope.project(project.id)
        m = self.memory
        m.remember(scope=scope, kind=MemoryKind.REQUIREMENT, author="human", pinned=True, importance=1.0,
                   subject="problem-statement", title=first_line(project.problem_statement, 200),
                   body=project.problem_statement, source_key=f"project:{project.id}:problem")
        for i, crit in enumerate(project.acceptance_criteria):
            m.remember(scope=scope, kind=MemoryKind.REQUIREMENT, author="human", pinned=True, title=crit,
                       tags=["acceptance"], source_key=f"project:{project.id}:ac:{i}")
        for i, req in enumerate(getattr(project, "requirements", []) or []):
            m.remember(scope=scope, kind=MemoryKind.REQUIREMENT, author="human", title=req,
                       source_key=f"project:{project.id}:req:{i}")
        constraints = getattr(project, "constraints", None)
        for name, value in (constraints.model_dump() if constraints is not None else {}).items():
            if value in (None, "") or name == "token_budget":
                continue
            m.remember(scope=scope, kind=MemoryKind.CONVENTION, author="human", pinned=True,
                       subject=f"constraint.{name}", title=f"{name.replace('_', ' ').capitalize()}: {value}",
                       source_key=f"project:{project.id}:constraint:{name}:{value}")
        self._synced_project = pid is not None

    def sync_decisions(self) -> None:
        if self.store is None:
            return
        pid = self._pid()
        if pid is None:
            return
        for d in self.store.list_decisions():
            self._decision(pid, decision=d.decision, context=d.context, reason=d.reason,
                           author=d.author, sprint_id=d.sprint_id, related=d.related_tasks,
                           source_key=f"decision:{d.id}", source_events=[])

    def sync_retrospectives(self) -> None:
        if self.store is None:
            return
        pid = self._pid()
        for sprint in self.store.list_sprints():
            if sprint.retrospective is not None and pid:
                self._retro(pid, sprint)

    # ------------------------------------------------------------ handlers
    def _decision(
        self, pid: str, *, decision: str, context: str, reason: str | None, author: str | None,
        sprint_id: str | None, related: Sequence[str], source_key: str, source_events: Sequence[str],
    ) -> None:
        ctx = (context or "").strip()
        low = ctx.lower()
        if not decision or low in OPERATIONAL_CONTEXTS or low.startswith("carry-forward"):
            return

        def remember(kind: MemoryKind, title: str, *, body: str = "", tags: Sequence[str] = (),
                     data: dict[str, Any] | None = None) -> None:
            self.memory.remember(
                scope=Scope.project(pid), kind=kind, title=title, body=body, tags=tags, data=data,
                author=author or self.curator, sprint_id=sprint_id, entities=list(related),
                source_key=source_key, source_events=list(source_events),
            )

        if low == "replanning":
            remember(MemoryKind.FACT, decision, tags=["replanning"])
        elif low.startswith("sprint") and "planning" in low:
            remember(MemoryKind.SUMMARY, f"{ctx}: {decision}", body=reason or "", tags=["sprint-plan"])
        else:
            remember(MemoryKind.DECISION, decision, body=f"Context: {ctx}" if ctx else "",
                     data={"rationale": reason or None, "context": ctx})

    def _on_project_created(self, e: Any, p: dict[str, Any]) -> None:
        self._pid(e)
        self.sync_project()

    def _on_backlog_created(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if pid and p.get("epic"):
            self.memory.remember(scope=Scope.project(pid), kind=MemoryKind.REQUIREMENT, author=self.curator,
                                 pinned=True, subject="epic", title=f"Epic: {p['epic']}",
                                 source_key=f"event:{e.id}", source_events=[e.id])

    def _on_decision_recorded(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not pid:
            return
        key = f"decision:{p['decision_id']}" if p.get("decision_id") else f"event:{e.id}"
        self._decision(pid, decision=str(p.get("decision", "")), context=str(p.get("context", "")),
                       reason=p.get("reason"), author=e.agent_id, sprint_id=e.sprint_id,
                       related=p.get("related_tasks") or [], source_key=key, source_events=[e.id])

    def _on_sprint_planned(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not pid:
            return
        self.memory.remember(
            scope=Scope.project(pid), kind=MemoryKind.SUMMARY, author=self.curator, pinned=True,
            subject="sprint-goal", title=f"Sprint {p.get('number', '?')} goal: {p.get('goal', '')}".strip(),
            entities=p.get("task_ids") or [], sprint_id=e.sprint_id, tags=["sprint-goal"],
            source_key=f"event:{e.id}", source_events=[e.id],
        )

    def _on_blocker_created(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not pid:
            return
        reason = str(p.get("reason", "blocked"))
        deps = _TASK_ID.findall(reason)
        scope = Scope.task(pid, e.task_id) if e.task_id else Scope.project(pid)
        author, role = self._assignee(e.task_id)
        self.memory.remember(
            scope=scope, kind=MemoryKind.BLOCKER, author=author, role=role, title=first_line(reason, 200),
            body=reason, entities=[x for x in (e.task_id, *deps) if x], sprint_id=e.sprint_id,
            tags=["dependency"] if reason.startswith("unmet dependencies") else [],
            source_key=f"event:{e.id}", source_events=[e.id],
        )

    def _resolve_blockers(self, pid: str, task_id: str, note: str) -> None:
        for b in self.engine.store.query(scopes=[Scope.task(pid, task_id)], kinds=[MemoryKind.BLOCKER]):
            self.memory.resolve(b.id, self.curator, note)

    def _on_blocker_resolved(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if pid and e.task_id:
            self._resolve_blockers(pid, e.task_id, str(p.get("note", "resolved")))

    def _on_task_completion_claimed(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not (pid and e.task_id):
            return
        author, role = self._assignee(e.task_id)
        attempt = p.get("attempt", 0)
        self.memory.remember(
            scope=Scope.task(pid, e.task_id), kind=MemoryKind.NOTE, author=author, role=role,
            title=f"completion claim (attempt {attempt})", body=str(p.get("summary", "")),
            tags=["claim"], entities=[e.task_id], sprint_id=e.sprint_id,
            source_key=f"claim:{e.task_id}:{attempt}", source_events=[e.id],
        )

    def _on_task_completed(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not (pid and e.task_id):
            return
        claims = self.engine.store.query(scopes=[Scope.task(pid, e.task_id)], kinds=[MemoryKind.NOTE],
                                         tags_any=["claim"], order="created")
        text = claims[-1].body if claims else ""
        author, role = self._assignee(e.task_id)
        commit = p.get("hash")
        report = HandoffReport.from_text(
            text, task_id=e.task_id, agent=author, commits=[commit] if commit else [],
            files_changed=self._repo_files(commit), evidence=["verification gate passed"],
        )
        team = role if role and "/" not in role else None
        self.engine.record_handoff(
            report, ReaderContext(project_id=pid, task_id=e.task_id, agent_id=author, role=role, team=team,
                                  sprint_id=e.sprint_id),
            source_key=f"event:{e.id}",
        )
        self._resolve_blockers(pid, e.task_id, "task completed")

    def _on_code_review_completed(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not (pid and e.task_id) or p.get("approved", True):
            return
        comments = [str(c) for c in p.get("comments") or []]
        self.memory.remember(
            scope=Scope.task(pid, e.task_id), kind=MemoryKind.FACT, author="reviewer", tags=["review"],
            title=f"Review requested changes ({p.get('severity', 'n/a')})",
            body="Code review requested changes:\n" + "\n".join(f"- {c}" for c in comments),
            importance=0.8, entities=[e.task_id], sprint_id=e.sprint_id,
            source_key=f"event:{e.id}", source_events=[e.id],
        )

    def _on_merge_conflict(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not (pid and e.task_id):
            return
        conflicts = [str(c) for c in p.get("conflicts") or []]
        self.memory.remember(
            scope=Scope.task(pid, e.task_id), kind=MemoryKind.FACT, author="system", tags=["integration"],
            title=f"Integration into main failed: {first_line(str(p.get('note', '')), 160)}",
            body=("Conflicting files: " + ", ".join(conflicts)) if conflicts else str(p.get("note", "")),
            importance=0.8, entities=[e.task_id, *conflicts], sprint_id=e.sprint_id,
            source_key=f"event:{e.id}", source_events=[e.id],
        )

    def _fallback_attempt(self, e: Any, attempt: int, outcome: str, detail: str) -> None:
        pid = self._pid(e)
        if not (self.attempts_from_events and pid and e.task_id):
            return
        author, role = self._assignee(e.task_id)
        reader = ReaderContext(project_id=pid, task_id=e.task_id, agent_id=author, role=role, sprint_id=e.sprint_id)
        self.engine.record_attempt(AttemptSummary(attempt=attempt, outcome=outcome, errors=[detail] if detail else []),
                                   reader, source_key=f"event:{e.id}:attempt")

    def _on_agent_failed(self, e: Any, p: dict[str, Any]) -> None:
        self._fallback_attempt(e, int(p.get("attempt", 0)), "error", str(p.get("error") or ""))

    def _on_task_verified(self, e: Any, p: dict[str, Any]) -> None:
        if not p.get("passed", True):
            self._fallback_attempt(e, int(p.get("attempt", 0)), "verify_failed", "")

    def _on_standup_completed(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not pid or not p.get("summary"):
            return
        self.memory.remember(
            scope=Scope.project(pid), kind=MemoryKind.SUMMARY, author=self.curator, subject="standup-latest",
            title=f"Standup {p.get('tick', '?')}: {first_line(str(p['summary']), 200)}", body=str(p["summary"]),
            tags=["standup"], sprint_id=e.sprint_id, source_key=f"event:{e.id}", source_events=[e.id],
        )

    def _retro(self, pid: str, sprint: Any) -> None:
        retro = sprint.retrospective
        cats: dict[str, list[str]] = {"went_well": [], "went_poorly": [], "change": []}
        for item in retro.items:
            cats.setdefault(item.category, []).append(item.text)
        self.engine.lessons.ingest_retrospective(
            pid, sprint_id=sprint.id, went_well=cats["went_well"], went_poorly=cats["went_poorly"],
            changes=cats["change"], action_items=list(retro.action_items), source_prefix=f"retro:{sprint.id}",
        )

    def _on_retrospective_completed(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if not pid:
            return
        sprint = self.store.get_sprint(e.sprint_id) if (self.store is not None and e.sprint_id) else None
        if sprint is not None and sprint.retrospective is not None:
            self._retro(pid, sprint)
        else:
            self.engine.lessons.ingest_retrospective(
                pid, sprint_id=e.sprint_id, action_items=[str(a) for a in p.get("action_items") or []],
                source_prefix=f"retro:{e.sprint_id}",
            )

    def _on_escalated_to_human(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        reason = str(p.get("note") or p.get("reason") or "escalated")
        if not pid or "human control" in reason:
            return
        if e.task_id:
            author, role = self._assignee(e.task_id)
            self.memory.remember(scope=Scope.task(pid, e.task_id), kind=MemoryKind.BLOCKER, author=author,
                                 role=role, title=f"Escalated: {first_line(reason, 180)}", body=reason,
                                 tags=["escalated"], entities=[e.task_id], sprint_id=e.sprint_id,
                                 source_key=f"event:{e.id}", source_events=[e.id])
        else:
            self.memory.remember(scope=Scope.project(pid), kind=MemoryKind.RISK, author=self.curator,
                                 title=first_line(reason, 200), body=reason, tags=["escalated"],
                                 sprint_id=e.sprint_id, source_key=f"event:{e.id}", source_events=[e.id])

    def _on_new_task_proposed(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        if pid:
            self.memory.remember(scope=Scope.project(pid), kind=MemoryKind.ACTION_ITEM, author=e.agent_id or "system",
                                 title=str(p.get("title") or p.get("summary") or "new task proposed"),
                                 body=str(p.get("description", "")), tags=["task-proposal"],
                                 entities=[x for x in (e.task_id,) if x], sprint_id=e.sprint_id,
                                 source_key=f"event:{e.id}", source_events=[e.id])

    def _on_agent_message_sent(self, e: Any, p: dict[str, Any]) -> None:
        pid = self._pid(e)
        sender = e.agent_id or p.get("sender")
        if not (pid and sender and p.get("content")):
            return
        ChannelMailbox(self.engine, pid, sender).send(
            str(p.get("recipient", "broadcast")), str(p.get("type", "MESSAGE")), str(p["content"]),
            related_tasks=p.get("related_tasks") or [], message_id=f"CM-{e.id}",   # replay-safe
        )

    # ---------------------------------------------------------------- briefs
    def reader_for(self, project: Any, task: Any, *, agent: Any = None, sprint: Any = None) -> ReaderContext:
        role = getattr(agent, "role", None) or getattr(task, "role", None)
        return ReaderContext(
            project_id=project.id, agent_id=getattr(agent, "id", None), role=role, team=role,
            task_id=task.id, sprint_id=getattr(sprint, "id", None) or getattr(task, "sprint_id", None),
            code_root=getattr(project, "repo_path", None) if self.code_outline else None,
        )

    @staticmethod
    def task_brief(task: Any) -> TaskBrief:
        return TaskBrief(
            objective=task.title, task_id=task.id, details=task.description or "",
            acceptance_criteria=[c.text for c in task.acceptance_criteria],
            dependencies=list(task.dependencies), report_instructions=None, escalate_when=[],
        )

    def brief_for_task(self, project: Any, task: Any, *, agent: Any = None, sprint: Any = None) -> str:
        """Supporting context for ``render_task_context`` (the task itself is rendered by aamt)."""
        if not self._synced_project:
            self._pid()
            self.sync_project()
        out = self.engine.build_brief(
            self.task_brief(task), self.reader_for(project, task, agent=agent, sprint=sprint),
            budget_tokens=self.brief_tokens, include_task_core=False, memory_index=self.memory_tools,
        )
        return out.text

    def task_context(self, project: Any, task: Any, *, agent: Any = None, sprint: Any = None) -> AamtTaskContext:
        return AamtTaskContext(self, self.reader_for(project, task, agent=agent, sprint=sprint))

    # -------------------------------------------------------------- reports
    def decision_log_markdown(self, project_id: str | None = None) -> str:
        pid = project_id or self._pid()
        if not pid:
            return "## Decisions\n\n_(no project)_\n"
        prefix = Scope.project(pid)
        recs = [r for r in self.engine.store.query(
            kinds=[MemoryKind.DECISION, MemoryKind.CONTRACT], order="created",
            statuses=[MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED, MemoryStatus.PROPOSED, MemoryStatus.REJECTED],
        ) if r.scope == prefix or r.scope.startswith(prefix + "/")]
        if not recs:
            return "## Decisions\n\n_(none recorded)_\n"
        lines = ["## Decisions"]
        for r in recs:
            why = f" — because {r.data['rationale']}" if r.data.get("rationale") else ""
            tail = f" (superseded by {r.superseded_by})" if r.superseded_by else ""
            lines.append(f"- [{r.status.value}] {r.kind.value}: {r.title}{why} _(by {r.author}){tail}_")
        return "\n".join(lines) + "\n"


@dataclass
class AamtTaskContext:
    """Hooks for one task's developer-agent runs (one ``BaseAgent.run`` = one run)."""

    bridge: AamtContextBridge
    reader: ReaderContext
    runs: int = 0
    session: ContextSession | None = field(default=None, repr=False)
    uid: str = field(default_factory=lambda: new_id("ctx", 6))   # one per run_task call

    def instrument(self, tools: Sequence[Any]) -> tuple[list[Any], Callable[[Any], dict[str, Any]]]:
        """Start a new run: returns (wrapped tools, pre_model_hook) for ``create_react_agent``."""
        from .langchain import make_pre_model_hook, memory_tools, wrap_tools

        self.runs += 1
        self.session = self.bridge.engine.session(f"{self.reader.task_id}#{self.uid}#run{self.runs}")
        wrapped = wrap_tools(tools, self.session)
        if self.bridge.memory_tools:
            wrapped += memory_tools(self.bridge.engine, self.reader)
        return wrapped, make_pre_model_hook(self.session)

    def finish_run(self, messages: Sequence[Any]) -> None:
        """Mirror the final transcript (the last model reply comes after the last hook call)."""
        from .langchain import entries_from_messages

        if self.session is not None:
            self.session.sync(entries_from_messages(messages))

    def record_attempt(self, attempt: int, *, outcome: str, feedback: str = "") -> None:
        entries = self.session.log.entries if self.session is not None else []
        summary = summarize_attempt(entries, attempt=attempt, outcome=outcome, feedback=feedback)
        self.bridge.engine.record_attempt(summary, self.reader, source_key=f"attempt:{self.uid}:{attempt}")

    def retry_context(self, feedback: str = "") -> str:
        """Earlier attempts on this task (compressed) + the current feedback, for ``extra_context``."""
        prior = self.bridge.engine.attempts(self.reader)[-3:]
        parts = []
        if prior:
            rendered = [AttemptSummary.model_validate(r.data).render(include_feedback=False) if r.data.get("attempt") is not None
                        else r.body for r in prior]
            parts.append("Earlier attempts on this task:\n" + "\n\n".join(rendered))
        if feedback:
            parts.append(feedback)
        return "\n\n".join(parts)


class ChannelMailbox:
    """``aamt.agents.messaging.Mailbox`` look-alike on channels with per-reader cursors.

    ``inbox()`` returns :class:`~aamt_context.types.ChannelMessage` (``recipients`` list and
    ``related`` instead of aamt's ``recipient`` / ``related_tasks``).
    """

    def __init__(self, engine: ContextEngine, project_id: str, agent_id: str, *, team: str | None = None,
                 aliases: Sequence[str] = ()):
        self.engine = engine
        self.project_id = project_id
        self.agent_id = agent_id
        self.team = team
        self.aliases = [*aliases, *([team] if team else [])]

    @property
    def channels(self) -> list[str]:
        return self.engine.channels.subscriptions(project_id=self.project_id, agent_id=self.agent_id, team=self.team)

    def send(self, recipient: str, type: str, content: str, *, related_tasks: Sequence[str] | None = None,
             message_id: str | None = None) -> ChannelMessage:
        hub = self.engine.channels
        if recipient == "broadcast":
            return hub.post(Channels.project(self.project_id), self.agent_id, content, type=type,
                            related=related_tasks or [], message_id=message_id)
        return hub.send(self.agent_id, recipient, content, type=type, related=related_tasks or [],
                        message_id=message_id)

    def broadcast(self, type: str, content: str, **kw: Any) -> ChannelMessage:
        return self.send("broadcast", type, content, **kw)

    def inbox(self, *, unread_only: bool = True) -> list[ChannelMessage]:
        hub = self.engine.channels
        if unread_only:
            return hub.unread(self.agent_id, self.channels, aliases=self.aliases)
        alias_set = {self.agent_id, *self.aliases}
        msgs = [m for ch in self.channels for m in hub.history(ch)
                if m.sender != self.agent_id and m.addressed_to(alias_set)]
        return sorted(msgs, key=lambda m: m.seq)

    def mark_read(self, *messages: ChannelMessage) -> None:
        self.engine.channels.ack(self.agent_id, messages)

    def drain(self) -> list[ChannelMessage]:
        msgs = self.inbox(unread_only=True)
        self.mark_read(*msgs)
        return msgs
