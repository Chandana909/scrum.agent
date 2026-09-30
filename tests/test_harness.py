from __future__ import annotations

from aamt_context.config import BudgetConfig
from aamt_context.harness import AgentLoop, ModelTurn, is_context_overflow, tool
from aamt_context.session import ContextSession
from aamt_context.worklog import EntryKind, ToolCall

BUDGET = BudgetConfig(window_tokens=4_000, reserve_output_tokens=500, tool_schema_tokens=0,
                      max_tool_result_tokens=400, keep_last_tool_results=2)


class ScriptedModel:
    def __init__(self, turns, *, fail_first_with: Exception | None = None):
        self.turns = list(turns)
        self.views: list[list] = []
        self.fail = fail_first_with

    def generate(self, entries, tools):
        if self.fail is not None:
            err, self.fail = self.fail, None
            raise err
        self.views.append(list(entries))
        return self.turns.pop(0)


def read_call(i: int, path: str) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(id=f"c{i}", name="read_file", args={"path": path})])


@tool
def read_file(path: str) -> str:
    """Read a file."""
    return f"# {path}\n" + "code line\n" * 400


@tool
def explode() -> str:
    """Always fails."""
    raise RuntimeError("disk on fire")


def test_loop_condenses_mid_run_and_finishes(store):
    turns = [read_call(i, f"src/m{i}.py") for i in range(10)]
    turns.append(ModelTurn(tool_calls=[ToolCall(id="x", name="explode"), ToolCall(id="y", name="nope")]))
    turns.append(ModelTurn(content="Done: read everything."))
    model = ScriptedModel(turns)
    events = []
    sess = ContextSession("loop-1", budget=BUDGET, store=store)
    res = AgentLoop(model, [read_file, explode], sess, on_event=events.append).run(
        "# Task T-9: read the modules", system="You are a careful engineer.")
    assert res.ok and res.final == "Done: read everything." and res.stop_reason == "final"
    assert res.steps == 12
    # every tool output was clipped at the source and restorable
    first_result = next(e for e in sess.log.entries if e.kind is EntryKind.TOOL)
    assert first_result.meta["clipped"] and first_result.meta["blob_id"]
    # the model never saw more than the hard limit
    counter = sess.counter
    assert all(sum(counter.count(e.content) for e in v) < BUDGET.hard_limit for v in model.views)
    assert any(ev.kind == "condense" for ev in events)
    errors = [e.content for e in sess.log.entries if e.kind is EntryKind.TOOL and e.content.startswith("ERROR")]
    assert any("disk on fire" in e for e in errors) and any("unknown tool 'nope'" in e for e in errors)
    # read_stored_output was exposed automatically
    assert "read_stored_output" in [t for t in AgentLoop(model, [read_file], sess).tools]


def test_overflow_error_forces_condensation_and_retries_once(store):
    model = ScriptedModel([ModelTurn(content="ok")],
                          fail_first_with=RuntimeError("This model's maximum context length is 8192 tokens"))
    sess = ContextSession("loop-2", budget=BUDGET, store=store)
    res = AgentLoop(model, [], sess).run("# Task")
    assert res.ok and res.final == "ok"
    assert is_context_overflow(RuntimeError("prompt is too long: 250000 tokens"))
    assert not is_context_overflow(RuntimeError("rate limited"))


def test_step_budget_and_model_errors(store):
    sess = ContextSession("loop-3", budget=BUDGET, store=store)
    res = AgentLoop(ScriptedModel([read_call(i, "a.py") for i in range(5)]), [read_file], sess, max_steps=3).run("# T")
    assert not res.ok and res.stop_reason == "max_steps" and res.tool_calls == ["read_file"] * 3

    class Broken:
        def generate(self, entries, tools):
            raise ValueError("bad request")

    res2 = AgentLoop(Broken(), [], ContextSession("loop-4", budget=BUDGET)).run("# T")
    assert not res2.ok and res2.stop_reason == "error" and "bad request" in res2.error


def test_tool_decorator_builds_schema():
    assert read_file.spec.parameters == {"type": "object", "properties": {"path": {"type": "string"}},
                                         "required": ["path"]}
    assert read_file.spec.description == "Read a file."
