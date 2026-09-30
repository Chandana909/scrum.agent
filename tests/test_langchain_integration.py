from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool as lc_tool
from pydantic import Field

from aamt_context.config import BudgetConfig
from aamt_context.engine import ContextEngine, ReaderContext
from aamt_context.harness import AgentLoop
from aamt_context.integrations.langchain import (
    LangChainChatModel,
    LangChainTextLLM,
    entries_from_messages,
    make_pre_model_hook,
    memory_tools,
    messages_from_entries,
    tool_from_langchain,
    wrap_tools,
)
from aamt_context.session import ContextSession
from aamt_context.types import MemoryKind, Scope
from aamt_context.worklog import EntryKind

BUDGET = BudgetConfig(window_tokens=4_000, reserve_output_tokens=500, tool_schema_tokens=0,
                      max_tool_result_tokens=400, keep_last_tool_results=2)


class ScriptedChatModel(BaseChatModel):
    responses: list[AIMessage] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        msg = self.responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs: Any):
        return self


@lc_tool
def read_file(path: str) -> str:
    """Read a UTF-8 text file relative to the repo root."""
    return f"# {path}\n" + "code line\n" * 400


def _calls(n: int) -> list[AIMessage]:
    out = [AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": f"src/m{i}.py"},
                                              "id": f"call{i}", "type": "tool_call"}]) for i in range(n)]
    return out + [AIMessage(content="All modules reviewed; nothing to change.")]


def test_roundtrip_preserves_ids_calls_and_checkpoints():
    msgs = [
        SystemMessage(content="sys", id="s"), HumanMessage(content="task", id="u"),
        AIMessage(content="", id="a", tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "c1",
                                                   "type": "tool_call"}]),
        ToolMessage(content="data", tool_call_id="c1", name="read_file", id="t"),
        HumanMessage(content=[{"type": "text", "text": "part1 "}, {"type": "text", "text": "part2"}], id="u2"),
    ]
    entries = entries_from_messages(msgs)
    assert [e.kind for e in entries] == [EntryKind.SYSTEM, EntryKind.USER, EntryKind.ASSISTANT, EntryKind.TOOL,
                                         EntryKind.USER]
    assert entries[2].tool_calls[0].args == {"path": "x"} and entries[3].tool_call_id == "c1"
    assert entries[4].content == "part1 part2"
    back = messages_from_entries(entries)
    assert [m.id for m in back] == ["s", "u", "a", "t", "u2"]
    assert back[2].tool_calls[0]["id"] == "c1" and isinstance(back[3], ToolMessage)
    # ids are stable for messages without ids
    no_ids = [HumanMessage(content="hi")]
    assert entries_from_messages(no_ids)[0].id == entries_from_messages(no_ids)[0].id


def test_pre_model_hook_condenses_what_the_model_sees_not_the_state(store):
    from langgraph.prebuilt import create_react_agent

    model = ScriptedChatModel(responses=_calls(10))
    session = ContextSession("lg-1", budget=BUDGET, store=store)
    tools = wrap_tools([read_file], session)
    agent = create_react_agent(model, tools, pre_model_hook=make_pre_model_hook(session))
    state = agent.invoke({"messages": [SystemMessage(content="You are an engineer."),
                                       HumanMessage(content="# Task T-1: review modules")]},
                         config={"recursion_limit": 60})
    # the graph state keeps every message (the log) ...
    assert len(state["messages"]) == 2 + 2 * 10 + 1
    assert state["messages"][-1].content == "All modules reviewed; nothing to change."
    # ... tool output was clipped at the source ...
    first_tool = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert "read_stored_output" in first_tool.content
    # ... and the model's input stayed bounded and valid
    counter = session.counter
    sizes = [sum(counter.count(str(m.content)) for m in seen) for seen in model.seen]
    assert max(sizes) < BUDGET.hard_limit
    last_input = model.seen[-1]
    assert isinstance(last_input[0], SystemMessage) and isinstance(last_input[1], HumanMessage)
    assert any(isinstance(m, HumanMessage) and m.name == "context_checkpoint" for m in last_input) or \
        any("cleared from context" in str(m.content) for m in last_input)
    assert session.stats()["edits"] + session.stats()["condensations"] >= 1


def test_middleware_for_langchain_v1_create_agent(store):
    agents = pytest.importorskip("langchain.agents")
    from aamt_context.integrations.langchain import ContextMiddleware

    model = ScriptedChatModel(responses=_calls(8))
    session = ContextSession("lc-1", budget=BUDGET, store=store)
    agent = agents.create_agent(model, wrap_tools([read_file], session), system_prompt="You are an engineer.",
                                middleware=[ContextMiddleware(session)])
    state = agent.invoke({"messages": [HumanMessage(content="# Task T-2: review")]}, config={"recursion_limit": 60})
    assert state["messages"][-1].content == "All modules reviewed; nothing to change."
    sizes = [sum(session.counter.count(str(m.content)) for m in seen) for seen in model.seen]
    assert max(sizes) < BUDGET.hard_limit
    assert session.log.entries[0].kind is EntryKind.SYSTEM


def test_wrapped_tools_clip_and_restore(store):
    session = ContextSession("tools-1", budget=BUDGET, store=store)
    tools = {t.name: t for t in wrap_tools([read_file], session)}
    out = tools["read_file"].invoke({"path": "big.py"})
    assert "clipped" in out
    blob = out.split('blob_id="')[1].split('"')[0]
    page = tools["read_stored_output"].invoke({"blob_id": blob, "offset": 0, "length": 30})
    assert page.startswith("# big.py")


def test_memory_tools_respect_visibility(store):
    engine = ContextEngine(store)
    engine.memory.remember(scope=Scope.project("P1"), kind=MemoryKind.DECISION, author="scrum-master",
                           title="Use JWT for auth")
    secret = engine.memory.remember(scope=Scope.project("P2"), kind=MemoryKind.DECISION, author="scrum-master",
                                    title="Use JWT for auth too").record
    reader = ReaderContext(project_id="P1", agent_id="A-be", task_id="T-1")
    tools = {t.name: t for t in memory_tools(engine, reader)}
    hits = tools["memory_search"].invoke({"query": "JWT auth"})
    assert "Use JWT for auth" in hits and secret.id not in hits
    rec_id = hits.split()[1]
    assert "Use JWT for auth" in tools["memory_read"].invoke({"record_id": rec_id})
    assert "no visible record" in tools["memory_read"].invoke({"record_id": secret.id})
    assert tools["memory_note"].invoke({"text": "remember to check bcrypt"}).startswith("noted as MR-")


def test_text_llm_and_chat_model_adapters(store):
    llm = LangChainTextLLM(ScriptedChatModel(responses=[AIMessage(content="## Goal\nsummary")]))
    assert llm.complete("system", "prompt", max_tokens=50) == "## Goal\nsummary"

    model = ScriptedChatModel(responses=_calls(2))
    session = ContextSession("h-1", budget=BUDGET, store=store)
    res = AgentLoop(LangChainChatModel(model), [tool_from_langchain(read_file)], session).run("# Task T-3")
    assert res.ok and res.tool_calls == ["read_file", "read_file"]
    assert res.final == "All modules reviewed; nothing to change."
