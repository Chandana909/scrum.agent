from __future__ import annotations

from aamt_context.worklog import (
    INCOMPLETE_TOOL_RESULT,
    Condensation,
    ContextEdit,
    Entry,
    EntryKind,
    ToolCall,
    WorkingLog,
    cut_points,
)


def _log() -> WorkingLog:
    log = WorkingLog()
    log.append(Entry(id="sys", kind=EntryKind.SYSTEM, content="system"))
    log.append(Entry(id="u1", kind=EntryKind.USER, content="task brief"))
    for i in range(1, 4):
        log.append(Entry(id=f"a{i}", kind=EntryKind.ASSISTANT,
                         tool_calls=[ToolCall(id=f"c{i}", name="read_file", args={"path": f"f{i}.py"})]))
        log.append(Entry(id=f"t{i}", kind=EntryKind.TOOL, tool_call_id=f"c{i}", name="read_file",
                         content=f"contents {i}"))
    return log


def ids(view):
    return [e.id for e in view]


def test_condensation_inserts_summary_where_the_range_was():
    log = _log()
    log.add_condensation(Condensation(id="C1", forgotten_ids=["a1", "t1", "a2", "t2"],
                                      summary="S1", after_id="u1"))
    assert ids(log.view()) == ["sys", "u1", "C1:summary", "a3", "t3"]
    assert log.view()[2].kind is EntryKind.SUMMARY and log.view()[2].content == "S1"
    # the raw log is untouched
    assert len(log.entries) == 8


def test_later_condensation_folds_the_earlier_summary():
    log = _log()
    log.add_condensation(Condensation(id="C1", forgotten_ids=["a1", "t1"], summary="S1", after_id="u1"))
    log.append(Entry(id="a4", kind=EntryKind.ASSISTANT, content="done"))
    log.add_condensation(Condensation(id="C2", forgotten_ids=["C1:summary", "a2", "t2", "a3", "t3"],
                                      summary="S2", after_id="u1"))
    assert ids(log.view()) == ["sys", "u1", "C2:summary", "a4"]


def test_edits_replace_or_omit_and_omit_turn_takes_results_along():
    log = _log()
    log.add_edit(ContextEdit(target_id="t1", replacement="[cleared]"))
    view = log.view()
    assert view[3].content == "[cleared]" and view[3].meta["edited"] == "mask"
    log.omit_turn("a2")
    assert "a2" not in ids(log.view()) and "t2" not in ids(log.view())
    new_calls = [ToolCall(id="c3", name="read_file", args={"path": "[elided]"})]
    log.add_edit(ContextEdit(target_id="a3", replacement_calls=new_calls, reason="elide_args"))
    a3 = next(e for e in log.view() if e.id == "a3")
    assert a3.tool_calls[0].args == {"path": "[elided]"}


def test_view_repairs_pairing():
    log = WorkingLog()
    log.append(Entry(id="u", kind=EntryKind.USER, content="go"))
    log.append(Entry(id="orphan", kind=EntryKind.TOOL, tool_call_id="zz", name="x", content="?"))
    log.append(Entry(id="a", kind=EntryKind.ASSISTANT,
                     tool_calls=[ToolCall(id="c1", name="x"), ToolCall(id="c2", name="y")]))
    log.append(Entry(id="t1", kind=EntryKind.TOOL, tool_call_id="c1", name="x", content="ok"))
    view = log.view()
    assert ids(view) == ["u", "a", "t1", "stub:c2"]
    assert view[-1].content == INCOMPLETE_TOOL_RESULT


def test_cut_points_never_split_a_call_from_its_result():
    view = _log().view()
    cuts = cut_points(view)
    assert all(view[i].kind is not EntryKind.TOOL for i in cuts if i < len(view))
    assert cuts[-1] == len(view)
