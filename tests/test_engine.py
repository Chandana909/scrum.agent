from __future__ import annotations

import pytest

from aamt_context.attempts import AttemptSummary
from aamt_context.briefs import HandoffReport, TaskBrief
from aamt_context.channels import Channels
from aamt_context.engine import ContextEngine, ReaderContext
from aamt_context.types import MemoryKind, MemoryStatus, Scope, Visibility, WriteAction

P = "P1"


@pytest.fixture
def engine(store, clock):
    return ContextEngine(store, clock=clock)


def _seed(engine: ContextEngine) -> dict[str, str]:
    m = engine.memory
    proj = Scope.project(P)
    ids = {}
    ids["req"] = m.remember(scope=proj, kind=MemoryKind.REQUIREMENT, author="human", pinned=True,
                            title="Problem: a task-list REST API with auth and SQLite").record.id
    ids["conv"] = m.remember(scope=proj, kind=MemoryKind.CONVENTION, author="scrum-master", pinned=True,
                             title="Language: Python 3.11; tests with pytest").record.id
    ids["dec"] = m.remember(scope=proj, kind=MemoryKind.DECISION, author="scrum-master", subject="auth.tokens",
                            title="Auth uses JWT bearer tokens", data={"rationale": "stateless API"},
                            entities=["src/auth.py"]).record.id
    ids["unrelated"] = m.remember(scope=proj, kind=MemoryKind.DECISION, author="scrum-master",
                                  title="Frontend uses Vite").record.id
    ids["lesson"] = m.remember(scope=Scope.team(P, "backend"), kind=MemoryKind.LESSON, author="scrum-master",
                               title="Run the full test suite before claiming a login change is done").record.id
    ids["clar"] = m.remember(scope=proj, kind=MemoryKind.CLARIFICATION, author="scrum-master",
                             title="Do login tokens expire?",
                             data={"question": "Do login tokens expire?", "answer": "Yes, after one hour"}).record.id
    ids["other_team_lesson"] = m.remember(scope=Scope.team(P, "frontend"), kind=MemoryKind.LESSON,
                                          author="scrum-master", title="Check login form accessibility").record.id
    return ids


def test_brief_pulls_the_right_memory_into_the_right_sections(engine, tmp_path):
    ids = _seed(engine)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login(user, pw):\n    ...\n", encoding="utf-8")
    engine.record_handoff(
        HandoffReport(task_id="T-1", agent="A-db", summary="users table + UserStore in src/store.py",
                      files_changed=["src/store.py"]),
        ReaderContext(project_id=P, task_id="T-1", agent_id="A-db", team="database"),
    )
    reader = ReaderContext(project_id=P, task_id="T-2", agent_id="A-be", role="backend", team="backend",
                           sprint_id="S-1", code_root=str(tmp_path))
    engine.record_attempt(AttemptSummary(attempt=1, outcome="verify_failed", files_modified=["src/auth.py"],
                                         failing_tests=["tests/test_auth.py::test_login"]), reader)
    engine.memory.remember(scope=reader.scope, kind=MemoryKind.FACT, author="reviewer", tags=["review"],
                           title="Review: hash passwords with bcrypt, not sha1")
    engine.channels.send("A-fe", "A-be", "Need the login response shape", type="QUESTION")

    brief = TaskBrief(objective="Implement POST /login with JWT", task_id="T-2", dependencies=["T-1"],
                      acceptance_criteria=["POST /login returns a JWT for valid credentials"])
    out = engine.build_brief(brief, reader)
    text = out.text
    keys = [s.key for s in out.sections]
    assert keys[0] == "task" and text.startswith("# Task T-2: Implement POST /login with JWT")
    assert keys.index("history") < keys.index("upstream") < keys.index("decisions")
    assert "Attempt 1 — verify_failed" in text and "bcrypt" in text
    assert "users table + UserStore in src/store.py" in text           # upstream handoff
    assert "Auth uses JWT bearer tokens — because stateless API" in text
    assert ids["dec"] in out.section("decisions").record_ids
    assert "Problem: a task-list REST API" in text and "Python 3.11" in text   # pinned
    assert "A: Yes, after one hour" in text
    assert "Run the full test suite before claiming a login change is done" in text
    assert "Check login form accessibility" not in text                # other team's lesson invisible
    assert "def login(user, pw)" in text                                # code outline
    assert "Need the login response shape" in text                     # inbox
    assert out.tokens <= engine.config.brief_tokens

    # inbox acked, accesses counted, manifest persisted
    assert engine.channels.unread("A-be", reader.channels()) == []
    assert engine.memory.get(ids["dec"]).access_count == 1
    manifests = engine.store.ctx_items("brief:T-2", kinds=["manifest"])
    assert manifests and manifests[0]["payload"]["reader"]["task_id"] == "T-2"


def test_tight_budget_keeps_task_and_drops_low_priority(engine):
    _seed(engine)
    reader = ReaderContext(project_id=P, task_id="T-3", agent_id="A-be", team="backend")
    brief = TaskBrief(objective="Implement POST /login with JWT", task_id="T-3", report_instructions=None)
    out = engine.build_brief(brief, reader, budget_tokens=160)
    assert out.section("task") is not None
    assert out.tokens <= 170
    assert set(out.dropped) & {"lessons", "risks", "clarifications"}


def test_include_filter_and_memory_index(engine):
    _seed(engine)
    reader = ReaderContext(project_id=P, task_id="T-4")
    out = engine.build_brief(TaskBrief(objective="login tokens", task_id="T-4"), reader,
                             include=["decisions", "index"], memory_index=True)
    assert {s.key for s in out.sections} <= {"task", "decisions", "index"}


def test_handoff_supersedes_previous_and_raises_proposals(engine):
    reader = ReaderContext(project_id=P, task_id="T-5", agent_id="A-be", role="backend/api", team="backend")
    first = engine.record_handoff(HandoffReport(task_id="T-5", agent="A-be", summary="v1"), reader)
    second = engine.record_handoff(HandoffReport(
        task_id="T-5", agent="A-be", summary="v2 after review",
        interfaces=["POST /login -> {token}"], decisions=["Use bcrypt for hashing"],
        follow_ups=["Add rate limiting"], open_questions=["Refresh tokens?"],
    ), reader)
    assert second[0].action is WriteAction.SUPERSEDED and second[0].related == [first[0].record.id]
    assert engine.latest_handoff("T-5").title == "T-5: v2 after review"
    kinds = {r.record.kind: r.record for r in second[1:]}
    assert kinds[MemoryKind.CONTRACT].status is MemoryStatus.PROPOSED       # sub-agent: needs curation
    assert kinds[MemoryKind.DECISION].status is MemoryStatus.PROPOSED
    assert kinds[MemoryKind.ACTION_ITEM].status is MemoryStatus.ACTIVE
    pending = engine.proposals(Visibility.build(P, team="backend"))
    assert {r.kind for r in pending} >= {MemoryKind.CONTRACT, MemoryKind.DECISION}
    announced = engine.channels.unread("A-fe", [Channels.project(P)])
    assert announced and announced[0].type == "API_CONTRACT_UPDATE"


def test_attempts_are_idempotent_only_per_caller_key(engine):
    reader = ReaderContext(project_id=P, task_id="T-6", agent_id="A-qa")
    s = AttemptSummary(attempt=1, outcome="error", errors=["ValueError: x"])
    assert engine.record_attempt(s, reader, source_key="attempt:run-a:1").action is WriteAction.ADDED
    assert engine.record_attempt(s, reader, source_key="attempt:run-a:1").action is WriteAction.DUPLICATE
    engine.record_attempt(AttemptSummary(attempt=2, outcome="verify_failed"), reader, source_key="attempt:run-a:2")
    # the task is re-run later: attempt numbers restart, but it is a new attempt
    engine.record_attempt(AttemptSummary(attempt=1, outcome="verify_failed", failing_tests=["t::x"]), reader,
                          source_key="attempt:run-b:1")
    assert [(r.data["attempt"], r.data["outcome"]) for r in engine.attempts(reader)] == [
        (1, "error"), (2, "verify_failed"), (1, "verify_failed")]


def test_session_factory_uses_engine_defaults(engine):
    s = engine.session("T-7#a1")
    s.system("sys")
    s.user("# Task T-7")
    assert s.budget == engine.config.budget and s.store is engine.store
    assert engine.store.ctx_items("T-7#a1")
