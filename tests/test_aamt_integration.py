"""Integration against the real aamt package (pinned commit), fully offline."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("aamt")
from aamt.agents.developer import render_task_context  # noqa: E402
from aamt.agents.messaging import Mailbox  # noqa: E402
from aamt.agents.roles import role_spec  # noqa: E402
from aamt.config import Settings  # noqa: E402
from aamt.events.bus import EventBus  # noqa: E402
from aamt.models import (  # noqa: E402
    AcceptanceCriterion,
    Agent,
    EventType,
    Project,
    ProjectConstraints,
    RetroItem,
    Retrospective,
    Sprint,
    Task,
)
from aamt.state.store import ProjectStore  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from aamt_context.engine import ContextEngine  # noqa: E402
from aamt_context.integrations.aamt import AamtContextBridge, ChannelMailbox  # noqa: E402
from aamt_context.types import MemoryKind, MemoryStatus, Scope  # noqa: E402

from .test_langchain_integration import ScriptedChatModel  # noqa: E402


@pytest.fixture
def env(tmp_path):
    store = ProjectStore(tmp_path / "state.db")
    bus = EventBus(tmp_path / "events.db")
    engine = ContextEngine.open(tmp_path / "context.db")
    bridge = AamtContextBridge(engine)
    bridge.attach(bus, store=store)
    project = Project(name="demo", problem_statement="Build a task-list REST API with auth and SQLite",
                      acceptance_criteria=["pytest passes"],
                      constraints=ProjectConstraints(language="python", framework="flask"),
                      repo_path=str(tmp_path / "repo"))
    store.save_project(project)
    backend = Agent(spec=role_spec("backend"))
    db = Agent(spec=role_spec("database"))
    store.save_agent(backend)
    store.save_agent(db)
    t1 = Task(title="Create users table and UserStore", role="database", assignee=db.id)
    t2 = Task(title="Implement POST /login", role="backend", assignee=backend.id, dependencies=[t1.id],
              description="Issue a JWT for valid credentials using the UserStore",
              acceptance_criteria=[AcceptanceCriterion(text="POST /login returns 200 with a token")])
    store.save_tasks([t1, t2])
    yield dict(store=store, bus=bus, engine=engine, bridge=bridge, project=project, backend=backend, db=db,
               t1=t1, t2=t2)
    engine.close()
    bus.close()
    store.close()


def _emit_sprint(env: dict[str, Any]) -> None:
    bus, p, t1, t2 = env["bus"], env["project"], env["t1"], env["t2"]
    bus.emit(EventType.PROJECT_CREATED, project_id=p.id, name="demo")
    bus.emit(EventType.DECISION_RECORDED, project_id=p.id, decision="Use SQLite via the stdlib sqlite3 module",
             context="architecture", reason="zero ops for the MVP", decision_id="D-1")
    bus.emit(EventType.DECISION_RECORDED, project_id=p.id, decision="final report written to x.md", context="reporting")
    bus.emit(EventType.SPRINT_PLANNED, project_id=p.id, sprint_id="S-1", number=1, goal="Auth on a user store",
             task_ids=[t1.id, t2.id], deferred=[])
    bus.emit_dict({"type": "TASK_COMPLETION_CLAIMED", "payload": {
        "task_id": t1.id, "attempt": 1,
        "summary": "SUMMARY: users table + UserStore(get_by_name, create)\nFILES: src/store.py, tests/test_store.py"}},
        project_id=p.id, sprint_id="S-1")
    bus.emit_dict({"type": "TASK_COMPLETED", "payload": {"task_id": t1.id, "hash": "deadbeef"}},
                  project_id=p.id, sprint_id="S-1")
    bus.emit(EventType.BLOCKER_CREATED, project_id=p.id, sprint_id="S-1", task_id=t2.id,
             reason=f"unmet dependencies: ['{t1.id}']")
    bus.emit(EventType.CODE_REVIEW_COMPLETED, project_id=p.id, sprint_id="S-1", task_id=t2.id, approved=False,
             severity="major", comments=["Hash passwords with bcrypt, not sha1"])
    bus.emit(EventType.STANDUP_COMPLETED, sprint_id="S-1", tick=1, summary="Store done; login in review")


def test_aamt_mailbox_loses_broadcasts_channel_mailbox_does_not(env):
    store, engine = env["store"], env["engine"]
    Mailbox(store, "A-be").broadcast("API_CONTRACT_UPDATE", "GET /tasks now paginates")
    assert len(Mailbox(store, "A-fe").drain()) == 1
    assert Mailbox(store, "A-qa").drain() == []            # the bug: QA never sees it
    ChannelMailbox(engine, "P", "A-be").broadcast("API_CONTRACT_UPDATE", "GET /tasks now paginates")
    assert len(ChannelMailbox(engine, "P", "A-fe").drain()) == 1
    assert len(ChannelMailbox(engine, "P", "A-qa").drain()) == 1
    assert ChannelMailbox(engine, "P", "A-qa").drain() == []


def test_events_become_shared_memory(env):
    _emit_sprint(env)
    engine, bridge, p, t1, t2 = env["engine"], env["bridge"], env["project"], env["t1"], env["t2"]
    assert bridge.errors == []
    q = engine.store.query
    reqs = {r.title: r for r in q(kinds=[MemoryKind.REQUIREMENT])}
    assert reqs["Build a task-list REST API with auth and SQLite"].pinned
    assert "pytest passes" in reqs
    convs = {r.subject: r.title for r in q(kinds=[MemoryKind.CONVENTION])}
    assert convs == {"constraint.language": "Language: python", "constraint.framework": "Framework: flask"}
    [dec] = q(kinds=[MemoryKind.DECISION])
    assert dec.title == "Use SQLite via the stdlib sqlite3 module" and dec.data["rationale"] == "zero ops for the MVP"
    assert dec.source_key == "decision:D-1"
    goal = q(kinds=[MemoryKind.SUMMARY], subject="sprint-goal")[0]
    assert goal.pinned and goal.title == "Sprint 1 goal: Auth on a user store"
    handoff = engine.latest_handoff(t1.id)
    assert handoff.scope == Scope.task(p.id, t1.id)
    assert handoff.data["files_changed"] == ["src/store.py", "tests/test_store.py"]
    assert handoff.author == env["db"].id and "deadbeef" in handoff.body
    [review] = q(scopes=[Scope.task(p.id, t2.id)], kinds=[MemoryKind.FACT])
    assert "bcrypt" in review.body and "review" in review.tags
    [blocker] = q(scopes=[Scope.task(p.id, t2.id)], kinds=[MemoryKind.BLOCKER])
    assert t1.id.lower() in blocker.entities
    standup = q(kinds=[MemoryKind.SUMMARY], subject="standup-latest")[0]
    assert standup.scope == Scope.project(p.id)               # project id recovered without the event carrying it
    # replaying the whole event log is a no-op
    before = len(q(statuses=None))
    for ev in env["bus"].query():
        bridge.ingest_event(ev)
    assert len(q(statuses=None)) == before


def test_brief_for_aamt_task_composes_with_render_task_context(env):
    _emit_sprint(env)
    bridge, p, t2, backend = env["bridge"], env["project"], env["t2"], env["backend"]
    supporting = bridge.brief_for_task(p, t2, agent=backend)
    prompt = render_task_context(t2, project_summary=supporting)
    assert prompt.count("POST /login returns 200 with a token") == 1        # task rendered once, by aamt
    assert "users table + UserStore(get_by_name, create)" in prompt        # upstream handoff
    assert "Hash passwords with bcrypt" in prompt                          # review feedback on this task
    # a project-wide decision reaches the agent even though it shares no words with the task
    assert "Use SQLite via the stdlib sqlite3 module — because zero ops" in prompt
    assert "Build a task-list REST API" in prompt and "Language: python" in prompt


def test_retrospective_ingestion_uses_the_store(env):
    store, bus, engine, p = env["store"], env["bus"], env["engine"], env["project"]
    sprint = Sprint(number=1, retrospective=Retrospective(
        items=[RetroItem(category="went_poorly", text="Frontend waited on an API that changed twice"),
               RetroItem(category="went_well", text="Tests caught the integration bug early")],
        action_items=["Freeze API contracts before frontend work"]))
    store.save_sprint(sprint)
    bus.emit(EventType.RETROSPECTIVE_COMPLETED, project_id=p.id, sprint_id=sprint.id,
             action_items=sprint.retrospective.action_items, n_items=2)
    lessons = engine.store.query(kinds=[MemoryKind.LESSON])
    assert {l.title for l in lessons} == {"Frontend waited on an API that changed twice",
                                         "Tests caught the integration bug early"}
    assert [a.title for a in engine.lessons.open_action_items(p.id)] == ["Freeze API contracts before frontend work"]


def test_task_context_wraps_the_real_developer_toolset(env, tmp_path):
    from aamt.tools.toolset import build_developer_toolset
    from aamt.tools.workspace import Workspace
    from langgraph.prebuilt import create_react_agent

    bridge, p, t2, backend = env["bridge"], env["project"], env["t2"], env["backend"]
    ws = Workspace(tmp_path / "repo")
    ws.write_file("big.txt", "row\n" * 40_000)
    settings = Settings(data_dir=tmp_path / "d", workspace_dir=tmp_path / "w", test_command="python -m pytest -q")
    ctx = bridge.task_context(p, t2, agent=backend)
    tools, hook = ctx.instrument(build_developer_toolset(ws, settings=settings))
    names = [t.name for t in tools]
    assert names[:9] == ["list_files", "repo_tree", "read_file", "search_code", "write_file", "run_command",
                         "git_status", "git_diff", "run_test_suite"]
    assert names[-1] == "read_stored_output"

    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "big.txt"}, "id": "c1",
                                           "type": "tool_call"}]),
        AIMessage(content="Could not finish: tests fail."),
    ])
    agent = create_react_agent(model, tools, pre_model_hook=hook)
    state = agent.invoke({"messages": [SystemMessage(content="dev"), HumanMessage(content="# Task")]})
    tool_msg = next(m for m in state["messages"] if m.type == "tool")
    assert "clipped" in tool_msg.content and len(tool_msg.content) < 20_000     # vs 160 KB raw
    ctx.finish_run(state["messages"])
    assert ctx.session.log.entries[-1].content == "Could not finish: tests fail."
    ctx.record_attempt(1, outcome="verify_failed", feedback="The test suite is failing.")
    retry = ctx.retry_context("Your previous attempt did NOT pass verification.")
    assert retry.startswith("Earlier attempts on this task:\nAttempt 1 — verify_failed")
    assert retry.count("did NOT pass verification") == 1
    # a later re-run of the same task (reopened) starts again at attempt 1 without clobbering history
    ctx2 = bridge.task_context(p, t2, agent=backend)
    ctx2.record_attempt(1, outcome="error", feedback="RateLimitError")
    assert [r.data["outcome"] for r in bridge.engine.attempts(ctx2.reader)] == ["verify_failed", "error"]
    assert ctx2.uid != ctx.uid


def test_offline_orchestrator_run_with_bridge_attached(tmp_path, monkeypatch):
    """aamt's own offline flow (tests/test_orchestrator_flow.py) with the bridge listening."""
    from aamt.orchestrator import Orchestrator
    from aamt.planning.schemas import BacklogPlan, PlannedCriterion, PlannedFeature, PlannedStory, PlannedTask
    from aamt.runtime.task_graph import TaskRunResult
    from aamt.tools.workspace import Workspace

    plan = BacklogPlan(epic="Task list", features=[PlannedFeature(title="Core", stories=[PlannedStory(
        title="Store", tasks=[
            PlannedTask(ref="t1", title="Create store module", description="in-memory store", role="backend",
                        priority="HIGH", estimate=2, acceptance_criteria=[PlannedCriterion(text="module exists")]),
            PlannedTask(ref="t2", title="Add tests for store", description="pytest", role="qa", priority="HIGH",
                        estimate=2, depends_on=["t1"], acceptance_criteria=[PlannedCriterion(text="tests pass")]),
        ])])])
    monkeypatch.setattr("aamt.agents.scrum_master.BacklogPlanner.generate", lambda self, project: plan)
    monkeypatch.setattr("aamt.agents.scrum_master.ScrumMaster._goal_sentence",
                        lambda self, project, plan: "Deliver the store slice.")
    briefs: dict[str, str] = {}

    def fake_run_task(task: Task, *, workspace_root, agent_role, **kw):
        briefs[task.title] = kw.get("project_summary", "")
        ws = Workspace(workspace_root)
        ws.ensure_git_repo()
        branch = f"task/{task.id}"
        ws.create_branch(branch)
        ws.write_file(f"src/{task.id}.py", f"# {task.title}\nvalue = 1\n")
        ws.commit_all(f"feat: {task.title}")
        t = task.model_copy(deep=True)
        from aamt.models.enums import TaskStatus
        t.advance_to(TaskStatus.IN_PROGRESS, actor=agent_role)
        t.advance_to(TaskStatus.TESTING, actor=agent_role)
        t.transition(TaskStatus.DONE, actor=agent_role)
        commit = ws.last_commit_hash()
        t.add_evidence(commit=commit, actor=agent_role)
        t.branch = branch
        return TaskRunResult(task=t, outcome="done", attempts=1, branch=branch, commit_hash=commit,
                             agent_summary="done", events=[
                                 {"type": "TASK_COMPLETION_CLAIMED", "payload": {
                                     "task_id": t.id, "attempt": 1, "summary": f"SUMMARY: {task.title} delivered"}},
                                 {"type": "TASK_COMPLETED", "payload": {"task_id": t.id, "hash": commit}}])

    monkeypatch.setattr("aamt.orchestrator.orchestrator.run_task", fake_run_task)
    monkeypatch.setattr("aamt.orchestrator.orchestrator.ReviewerAgent.review", lambda self, task, ws, **kw: __import__(
        "aamt.integration.reviewer", fromlist=["ReviewResult"]).ReviewResult(approved=True, comments=["ok"]))
    settings = Settings(data_dir=tmp_path / "d", workspace_dir=tmp_path / "w", enable_standups=False,
                        enable_retro=False, enable_llm_judge=False, inter_task_seconds=0, max_sprints=3,
                        test_command="python -m pytest -q")
    settings.ensure_dirs()
    monkeypatch.setattr("aamt.config.get_settings", lambda reload=False: settings)

    orch = Orchestrator(settings)
    engine = ContextEngine.open(settings.data_dir / "context.db")
    bridge = AamtContextBridge(engine)
    bridge.attach(orch.bus, store=orch.store)
    project = orch.run(name="demo", problem_statement="build a task list store", repo_path=str(tmp_path / "repo"),
                       acceptance_criteria=["pytest passes"], max_sprints=3)
    assert project.status.value == "COMPLETED" and bridge.errors == []

    tasks = {t.title: t for t in orch.store.list_tasks() if t.kind.value == "TASK"}
    h1 = engine.latest_handoff(tasks["Create store module"].id)
    assert h1 is not None and h1.data["files_changed"] == [f"src/{tasks['Create store module'].id}.py"]
    assert h1.status is MemoryStatus.ACTIVE
    # the dependent task's brief, built after the fact, carries t1's handoff
    brief = bridge.brief_for_task(project, tasks["Add tests for store"])
    assert "Create store module delivered" in brief
    assert engine.store.query(kinds=[MemoryKind.SUMMARY], subject="sprint-goal")
    assert "Epic: Task list" in {r.title for r in engine.store.query(kinds=[MemoryKind.REQUIREMENT])}
    engine.close()
    orch.close()
