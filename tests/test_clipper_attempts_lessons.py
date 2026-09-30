from __future__ import annotations

import json

from aamt_context.attempts import summarize_attempt
from aamt_context.clipper import ToolOutputClipper
from aamt_context.lessons import LessonConsolidator
from aamt_context.types import MemoryKind, MemoryStatus, Scope, WriteAction
from aamt_context.worklog import Entry, EntryKind, ToolCall

from .conftest import ScriptedLLM


def test_clipper_keeps_short_output_and_clips_long_with_restorable_blob(store):
    clipper = ToolOutputClipper(store=store, max_tokens=100)
    assert clipper.clip("read_file", "short").clipped is False
    body = "HEAD\n" + ("filler line\n" * 300) + "ValueError: bad input\n" + ("more\n" * 300) + "TAIL"
    res = clipper.clip("read_file", body, session_id="s")
    assert res.clipped and res.blob_id
    assert res.text.startswith("HEAD") and res.text.endswith("TAIL")
    assert "ValueError: bad input" in res.text                   # salvaged from the middle
    assert len(res.text) < len(body) // 3
    page = clipper.read(res.blob_id, offset=0, length=50)
    assert page.startswith("HEAD") and "offset=50" in page


def test_clipper_is_tail_heavy_for_test_runs(store):
    clipper = ToolOutputClipper(store=store, max_tokens=100)
    out = "collecting...\n" + "." * 3_000 + "\n=== 3 failed, 10 passed in 1.2s ==="
    res = clipper.clip("run_test_suite", out)
    head, _, tail = res.text.partition("[…")
    assert len(tail) > len(head)
    assert res.text.endswith("3 failed, 10 passed in 1.2s ===")


def test_attempt_summary_extracts_what_matters():
    entries = [
        Entry(kind=EntryKind.USER, content="# Task T-1"),
        Entry(kind=EntryKind.ASSISTANT, tool_calls=[ToolCall(id="1", name="read_file", args={"path": "src/api.py"})]),
        Entry(kind=EntryKind.TOOL, tool_call_id="1", name="read_file", content="def app(): ..."),
        Entry(kind=EntryKind.ASSISTANT, tool_calls=[ToolCall(id="2", name="write_file",
                                                             args={"path": "src/api.py", "content": "x"})]),
        Entry(kind=EntryKind.TOOL, tool_call_id="2", name="write_file", content="wrote src/api.py"),
        Entry(kind=EntryKind.ASSISTANT, tool_calls=[ToolCall(id="3", name="run_test_suite")]),
        Entry(kind=EntryKind.TOOL, tool_call_id="3", name="run_test_suite",
              content="tests: FAIL (exit=1, 2 passed, 1 failed)\n"
                      "FAILED tests/test_api.py::test_create - KeyError: 'title'\n"
                      "E   KeyError: 'title'"),
        Entry(kind=EntryKind.ASSISTANT, content="Implemented POST /tasks; tests pass."),
    ]
    s = summarize_attempt(entries, attempt=1, outcome="verify_failed",
                          feedback="Your previous attempt did NOT pass verification.")
    assert s.files_modified == ["src/api.py"] and s.files_read == []
    assert s.failing_tests == ["tests/test_api.py::test_create"]
    assert s.test_status.startswith("tests: FAIL")
    assert any("KeyError" in e for e in s.errors)
    text = s.render()
    assert "Attempt 1 — verify_failed" in text and "tests/test_api.py::test_create" in text
    assert s.title().startswith("Attempt 1 verify_failed: tests: FAIL")


def test_retro_lessons_are_scoped_deduplicated_and_idempotent(memory):
    lc = LessonConsolidator(memory)
    r1 = lc.ingest_retrospective(
        "P1", sprint_id="S-1",
        went_poorly=["Frontend waited on an API that changed twice", "The CI pipeline was flaky"],
        went_well=["Tests caught the integration bug early"],
        action_items=["Freeze API contracts before frontend work"],
        source_prefix="retro:S-1",
    )
    assert [r.action for r in r1] == [WriteAction.ADDED] * 4
    by_title = {r.record.title: r.record for r in r1}
    assert by_title["The CI pipeline was flaky"].scope == Scope.team("P1", "devops")
    assert by_title["Frontend waited on an API that changed twice"].scope == Scope.team("P1", "frontend")
    # replaying the same retro is a no-op
    again = lc.ingest_retrospective("P1", went_poorly=["Frontend waited on an API that changed twice"],
                                    source_prefix="retro:S-1")
    assert again[0].action is WriteAction.DUPLICATE
    # the same lesson in a later sprint strengthens the existing record
    r2 = lc.ingest_retrospective("P1", sprint_id="S-2", went_poorly=["The CI pipeline was flaky"])
    assert r2[0].action is WriteAction.DUPLICATE and r2[0].record.support == 2
    assert [a.title for a in lc.open_action_items("P1")] == ["Freeze API contracts before frontend work"]


def test_retro_llm_rewrite_path(memory):
    llm = ScriptedLLM(json.dumps({"lessons": [
        {"lesson": "Agree API contracts before starting UI work", "polarity": "negative", "team": "frontend"},
        {"lesson": "", "polarity": "negative"},
    ]}))
    res = LessonConsolidator(memory, llm=llm).ingest_retrospective("P1", went_poorly=["UI blocked on API changes"])
    assert len(res) == 1
    rec = res[0].record
    assert rec.kind is MemoryKind.LESSON and rec.status is MemoryStatus.ACTIVE
    assert rec.title == "Agree API contracts before starting UI work"
    assert rec.scope == Scope.team("P1", "frontend") and rec.data["polarity"] == "negative"
