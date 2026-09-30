from __future__ import annotations

import pytest

from aamt_context.assembly import ContextAssembler, Item, ListSection, TextSection, Tier, fit_text
from aamt_context.briefs import HandoffReport, TaskBrief, derive_child_brief
from aamt_context.codemap import CodeMap, outline_python
from aamt_context.tokens import HeuristicTokenCounter

C = HeuristicTokenCounter()


def test_fit_text_head_and_tail():
    text = "abcdefghij" * 40
    assert C.count(fit_text(text, 10, C)) <= 11 and fit_text(text, 10, C).startswith("abc")
    assert fit_text(text, 10, C, keep="tail").endswith("hij")
    assert fit_text("short", 10, C) == "short"


def test_assembler_prioritises_caps_and_orders_by_layout():
    sections = [
        ListSection("lessons", "Lessons", [Item(f"- lesson {i} " + "x" * 60, f"MR-l{i}") for i in range(20)],
                    tier=Tier.PROJECT, priority=10, order=30),
        TextSection("task", "Task", "Do the thing. " * 20, priority=100, order=0, required=True),
        ListSection("decisions", "Decisions", [Item(f"- decision {i}", f"MR-d{i}") for i in range(5)],
                    priority=80, order=10, max_share=0.3),
        TextSection("empty", "Empty", "   ", priority=90, order=5),
    ]
    out = ContextAssembler(C).assemble(sections, budget=300)
    assert [s.key for s in out.sections] == ["task", "decisions", "lessons"]   # layout order
    assert out.tokens <= 300 + 5
    lessons = out.section("lessons")
    assert lessons.truncated and lessons.shown_items < 20 and "more" in lessons.text
    assert out.empty == ["empty"] and "empty" not in out.dropped
    assert out.section("decisions").record_ids == [f"MR-d{i}" for i in range(5)]
    assert out.prefix_hash                                              # PROJECT-tier content hashed
    m = out.manifest()
    assert m["budget"] == 300 and {s["key"] for s in m["sections"]} == {"task", "decisions", "lessons"}


def test_required_section_survives_starvation():
    sections = [
        TextSection("noise", "Noise", "n " * 2_000, priority=99, order=1),
        TextSection("task", "Task", "Implement GET /tasks", priority=10, order=0, required=True),
    ]
    out = ContextAssembler(C).assemble(sections, budget=120)
    assert out.section("task") is not None and "Implement GET /tasks" in out.text


def test_brief_render_and_child_derivation():
    parent = TaskBrief(objective="Build the backend", task_id="T-be", ownership=["src/api", "src/auth"],
                       constraints=["Python 3.11", "no new dependencies"],
                       interfaces=["GET /tasks -> [{id,title,done}]"], max_steps=40)
    child = derive_child_brief(parent, objective="JWT login endpoint", task_id="T-jwt", ownership=["src/auth"],
                               acceptance_criteria=["POST /login returns a token"])
    assert child.parent_task_id == "T-be" and child.constraints == parent.constraints
    assert child.interfaces == parent.interfaces and child.max_steps == 20
    text = child.render_core()
    assert text.startswith("# Task T-jwt: JWT login endpoint\n(sub-task of T-be)")
    assert "You own (change only these)" in text and "SUMMARY:" in text
    with pytest.raises(ValueError):
        derive_child_brief(parent, objective="x", ownership=["src/frontend"])
    assert "src/auth/jwt.py" in TaskBrief(objective="fix src/auth/jwt.py").retrieval_entities()


def test_handoff_report_parses_markers():
    text = """Implemented login.
SUMMARY: POST /login issues HS256 JWTs
FILES: src/auth/jwt.py, tests/test_login.py
INTERFACE: POST /login {username,password} -> {token}
DECISION: tokens expire after 1h because the PRD says sessions are short
QUESTION: should refresh tokens exist?
FOLLOW-UP: add rate limiting to /login"""
    r = HandoffReport.from_text(text, task_id="T-jwt", agent="A-be", commits=["abcdef1234567"])
    assert r.summary == "POST /login issues HS256 JWTs"
    assert r.files_changed == ["src/auth/jwt.py", "tests/test_login.py"]
    assert r.interfaces == ["POST /login {username,password} -> {token}"]
    assert r.decisions and r.open_questions and r.follow_ups == ["add rate limiting to /login"]
    rendered = r.render()
    assert rendered.startswith("T-jwt (done, by A-be): POST /login issues HS256 JWTs")
    assert "abcdef1234" in rendered
    plain = HandoffReport.from_text("Added the endpoint and tests; all green.", task_id="T", agent="A")
    assert plain.summary == "Added the endpoint and tests; all green."


def test_outline_python_shows_api_not_bodies():
    src = '''"""Task store."""
import os
MAX_ITEMS = 100
class TaskStore(Base):
    def __init__(self, path): ...
    def add(self, title: str) -> int: ...
    def _private(self): ...
def create_app(config: dict | None = None) -> "App":
    return App()
def _helper(): ...
'''
    lines = outline_python(src)
    assert lines[0] == '"""Task store."""'
    assert "class TaskStore(Base): __init__, add" in lines
    assert "def create_app(config: dict | None=None) -> 'App'" in lines
    assert "MAX_ITEMS = …" in lines
    assert not any("_helper" in ln or "_private" in ln for ln in lines)
    assert outline_python("def broken(:")[0].startswith("(syntax error")


def test_codemap_ranks_relevant_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login(username, password):\n    ...\n", encoding="utf-8")
    (tmp_path / "src" / "tasks.py").write_text("def list_tasks():\n    ...\n", encoding="utf-8")
    (tmp_path / "tests" / "test_auth.py").write_text("def test_login():\n    ...\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    cm = CodeMap(tmp_path)
    assert ".venv/junk.py" not in cm.files()
    ranked = [p for p, _ in cm.rank("add rate limiting to the login endpoint")]
    assert ranked[0] == "src/auth.py"
    assert [p for p, _ in cm.rank("anything", hints=["src/tasks.py"])][0] == "src/tasks.py"
    text, shown = cm.render("login", 200)
    assert shown[0] == "src/auth.py" and "def login(username, password)" in text
    assert "of 3 files" in text
