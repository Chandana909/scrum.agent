from __future__ import annotations

import pytest

from aamt_context.condensers import (
    CHECKPOINT_HEADER,
    CondenserPipeline,
    SummarizingCondenser,
    ToolResultMasker,
)
from aamt_context.config import BudgetConfig
from aamt_context.session import ContextSession
from aamt_context.worklog import EntryKind, ToolCall

from .conftest import ScriptedLLM

SMALL = BudgetConfig(window_tokens=4_000, reserve_output_tokens=500, tool_schema_tokens=0,
                     max_tool_result_tokens=5_000, keep_last_tool_results=2)


def _session(store, *, pipeline, llm=None, budget=SMALL) -> ContextSession:
    s = ContextSession("run-1", budget=budget, store=store, pipeline=pipeline, llm=llm)
    s.system("You are a backend engineer.")
    s.user("# Task T-1: add /tasks endpoint\nAcceptance: GET /tasks returns 200")
    return s


def _turn(s: ContextSession, i: int, *, tool="read_file", size=2_400, content=None, args=None):
    call = ToolCall(id=f"c{i}", name=tool, args=args or {"path": f"src/m{i}.py"})
    s.assistant(f"step {i}", [call])
    s.tool_result(f"c{i}", tool, content if content is not None else f"line {i}\n" + "x" * size)


def test_masking_clears_oldest_results_first_and_keeps_recent(store):
    s = _session(store, pipeline=CondenserPipeline([ToolResultMasker()]))
    for i in range(6):
        _turn(s, i)
    before = s.raw_tokens()
    view = s.view()
    tools = [e for e in view if e.kind is EntryKind.TOOL]
    cleared = [e for e in tools if e.content.startswith("[read_file output cleared")]
    assert cleared and tools[0] in cleared
    assert not tools[-1].content.startswith("[") and not tools[-2].content.startswith("[")  # keep_last=2
    assert s.tokens() < SMALL.mask_trigger < before
    blob = cleared[0].content.split('blob_id="')[1].split('"')[0]
    assert "line 0" in s.read_stored_output(blob)                     # restorable
    assert s.condense() == []                                          # hysteresis: no churn next turn


def test_masking_respects_excluded_tools_and_elides_big_arguments(store):
    s = _session(store, pipeline=CondenserPipeline([ToolResultMasker(exclude_tools=("run_test_suite",))]))
    _turn(s, 0, tool="run_test_suite", content="tests: FAIL\n" + "y" * 2_400, args={})
    _turn(s, 1, tool="write_file", content="wrote src/a.py", args={"path": "src/a.py", "content": "z" * 6_000})
    for i in range(2, 5):
        _turn(s, i)
    view = s.view()
    test_result = next(e for e in view if e.name == "run_test_suite")
    assert test_result.content.startswith("tests: FAIL")
    write_call = next(e for e in view if e.tool_calls and e.tool_calls[0].name == "write_file")
    assert write_call.tool_calls[0].args["content"].startswith("[6000 chars elided")
    assert write_call.tool_calls[0].args["path"] == "src/a.py"


def test_summarizer_keeps_prefix_and_tail_and_uses_llm(store):
    llm = ScriptedLLM("## Goal\nadd /tasks\n## Progress\nread m0-m3", "## Goal\nadd /tasks (updated)")
    s = _session(store, pipeline=CondenserPipeline([SummarizingCondenser()]), llm=llm)
    for i in range(6):
        _turn(s, i, size=1_800)
    view = s.view()
    assert [e.kind for e in view[:2]] == [EntryKind.SYSTEM, EntryKind.USER]
    assert view[2].kind is EntryKind.SUMMARY and view[2].content.startswith(CHECKPOINT_HEADER)
    assert "read m0-m3" in view[2].content
    assert view[3].kind is EntryKind.ASSISTANT                       # tail starts on a turn boundary
    assert view[-1].id == s.log.entries[-1].id                        # latest result kept verbatim
    assert s.tokens() <= SMALL.soft_limit
    assert "<previous-checkpoint>" not in llm.prompts[0][1]
    for i in range(6, 12):
        _turn(s, i, size=1_800)
    s.view()
    assert "<previous-checkpoint>" in llm.prompts[1][1]             # iterative update
    assert len([e for e in s.view() if e.kind is EntryKind.SUMMARY]) == 1
    cond = s.log.condensations[-1]
    assert cond.method == "llm" and cond.tokens_after < cond.tokens_before


def test_summarizer_falls_back_to_extractive_checkpoint(store):
    s = _session(store, pipeline=CondenserPipeline([SummarizingCondenser()]), llm=ScriptedLLM(fail=True))
    _turn(s, 0, tool="write_file", content="wrote src/api.py", args={"path": "src/api.py", "content": "pass"})
    _turn(s, 1, tool="run_test_suite", args={},
          content="tests: FAIL (exit=1, 1 passed, 1 failed)\nFAILED tests/test_api.py::test_list - assert 404 == 200")
    for i in range(2, 8):
        _turn(s, i, size=1_800)
    s.view()
    cond = s.log.condensations[0]
    assert cond.method == "extractive"
    assert "src/api.py" in cond.summary
    assert "tests/test_api.py::test_list" in cond.summary
    assert "tests: FAIL (exit=1" in cond.summary


def test_hard_request_resets_even_a_single_giant_turn(store):
    s = _session(store, pipeline=CondenserPipeline([SummarizingCondenser()]))
    _turn(s, 0, size=200)
    assert s.condense() == []                                      # below limits: nothing to do
    assert s.condense(force=True) == ["summarize"]
    view = s.view()
    assert [e.kind for e in view] == [EntryKind.SYSTEM, EntryKind.USER, EntryKind.SUMMARY]


def test_session_persists_and_reloads(store):
    s = _session(store, pipeline=CondenserPipeline([ToolResultMasker(), SummarizingCondenser()]))
    for i in range(8):
        _turn(s, i)
    before = [e.model_dump() for e in s.view()]
    again = ContextSession.load(store, "run-1", budget=SMALL)
    assert [e.model_dump() for e in again.view(condense=False)] == before
    assert again.stats()["edits"] == s.stats()["edits"] >= 1
    assert again.stats()["condensations"] == s.stats()["condensations"]


def test_sync_is_idempotent_by_id(store):
    s = _session(store, pipeline=CondenserPipeline([]))
    entries = list(s.log.entries)
    assert s.sync(entries) == 0
    other = ContextSession("run-2", store=store, pipeline=CondenserPipeline([]))
    assert other.sync(entries) == 2 and other.goal == "# Task T-1: add /tasks endpoint"


@pytest.mark.parametrize("ratio", [0.5, 0.75])
def test_budget_properties(ratio):
    b = BudgetConfig(window_tokens=10_000, reserve_output_tokens=1_000, tool_schema_tokens=1_000, mask_ratio=ratio)
    assert b.available == 8_000 and b.mask_trigger == int(8_000 * ratio)
