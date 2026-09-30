"""LangChain / LangGraph adapters.

* message <-> entry conversion (ids preserved, so a session can mirror graph state);
* :func:`make_pre_model_hook` — plug a :class:`ContextSession` into
  ``langgraph.prebuilt.create_react_agent(..., pre_model_hook=...)``: the graph keeps the
  full transcript in its state, the model sees the session's condensed view
  (``llm_input_messages`` is not written back to ``messages``);
* :class:`ContextMiddleware` — the same for LangChain v1 ``create_agent`` middleware;
* :func:`wrap_tools` — clip tool output at the source (restorable) and add
  ``read_stored_output``;
* :func:`memory_tools` — just-in-time access to shared memory;
* :class:`LangChainTextLLM` / :class:`LangChainChatModel` — use a LangChain chat model as
  the summariser/extractor, or inside :class:`~aamt_context.harness.AgentLoop`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from .._util import first_line, new_id, stable_hash
from ..harness import ModelTurn, Tool, ToolSpec
from ..memory import render_index_line, render_record
from ..session import ContextSession
from ..types import MemoryKind, Scope
from ..worklog import Entry, EntryKind, ToolCall

CHECKPOINT_NAME = "context_checkpoint"


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def entries_from_messages(messages: Sequence[BaseMessage]) -> list[Entry]:
    out: list[Entry] = []
    for i, m in enumerate(messages):
        text = message_text(m.content)
        mid = m.id or f"lc:{i}:{m.type}:{stable_hash(text)}"
        if isinstance(m, SystemMessage):
            out.append(Entry(id=mid, kind=EntryKind.SYSTEM, content=text))
        elif isinstance(m, HumanMessage):
            kind = EntryKind.SUMMARY if m.name == CHECKPOINT_NAME else EntryKind.USER
            out.append(Entry(id=mid, kind=kind, content=text))
        elif isinstance(m, AIMessage):
            calls = [ToolCall(id=tc.get("id") or f"{mid}:call{j}", name=tc["name"], args=tc.get("args") or {})
                     for j, tc in enumerate(m.tool_calls or [])]
            out.append(Entry(id=mid, kind=EntryKind.ASSISTANT, content=text, tool_calls=calls))
        elif isinstance(m, ToolMessage):
            out.append(Entry(id=mid, kind=EntryKind.TOOL, content=text, tool_call_id=m.tool_call_id,
                             name=m.name))
        else:  # chat/function messages from older APIs: keep as user text
            out.append(Entry(id=mid, kind=EntryKind.USER, content=text))
    return out


def messages_from_entries(entries: Sequence[Entry]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for e in entries:
        if e.kind is EntryKind.SYSTEM:
            out.append(SystemMessage(content=e.content, id=e.id))
        elif e.kind is EntryKind.USER:
            out.append(HumanMessage(content=e.content, id=e.id))
        elif e.kind is EntryKind.SUMMARY:
            out.append(HumanMessage(content=e.content, id=e.id, name=CHECKPOINT_NAME))
        elif e.kind is EntryKind.ASSISTANT:
            out.append(AIMessage(
                content=e.content, id=e.id,
                tool_calls=[{"name": c.name, "args": c.args, "id": c.id, "type": "tool_call"} for c in e.tool_calls],
            ))
        elif e.kind is EntryKind.TOOL:
            out.append(ToolMessage(content=e.content, tool_call_id=e.tool_call_id or "", name=e.name, id=e.id))
    return out


# ---------------------------------------------------------------------------
# LangGraph prebuilt agent / LangChain v1 middleware
# ---------------------------------------------------------------------------
def make_pre_model_hook(session: ContextSession) -> Callable[[Any], dict[str, Any]]:
    """``pre_model_hook`` for ``create_react_agent``: mirror state, return the condensed view."""

    def hook(state: Any) -> dict[str, Any]:
        messages = state["messages"] if isinstance(state, dict) else state.messages
        session.sync(entries_from_messages(messages))
        return {"llm_input_messages": messages_from_entries(session.view())}

    return hook


try:  # LangChain v1 agents (optional)
    from langchain.agents.middleware import AgentMiddleware as _AgentMiddleware
except Exception:  # noqa: BLE001
    _AgentMiddleware = None  # type: ignore[assignment,misc]

if _AgentMiddleware is not None:

    class ContextMiddleware(_AgentMiddleware):  # type: ignore[misc,valid-type]
        """``create_agent(..., middleware=[ContextMiddleware(session)])``."""

        def __init__(self, session: ContextSession):
            super().__init__()
            self.session = session

        def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
            msgs = ([request.system_message] if request.system_message is not None else []) + list(request.messages)
            self.session.sync(entries_from_messages(msgs))
            view = [m for m in messages_from_entries(self.session.view()) if not isinstance(m, SystemMessage)]
            return handler(request.override(messages=view))


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
def _clipping(t: BaseTool, session: ContextSession) -> Callable[..., str]:
    def run(**kwargs: Any) -> str:
        out = t.invoke(kwargs)
        return session.clip_output(t.name, message_text(getattr(out, "content", out)))

    return run


def stored_output_tool(session: ContextSession) -> BaseTool:
    def read_stored_output(blob_id: str, offset: int = 0, length: int = 6_000) -> str:
        """Read more of a tool output that was clipped or cleared from your context.
        Use the blob_id shown in the clipped output; page forward with offset."""
        return session.read_stored_output(blob_id, offset=offset, length=length)

    return StructuredTool.from_function(read_stored_output)


def wrap_tools(tools: Sequence[BaseTool], session: ContextSession, *, add_reader: bool = True) -> list[BaseTool]:
    wrapped: list[BaseTool] = [
        StructuredTool.from_function(func=_clipping(t, session), name=t.name, description=t.description,
                                     args_schema=t.args_schema)
        for t in tools
    ]
    if add_reader and session.store is not None:
        wrapped.append(stored_output_tool(session))
    return wrapped


def memory_tools(engine: Any, reader: Any) -> list[BaseTool]:
    """Just-in-time memory access: search, read one record, keep a private note."""
    vis = reader.visibility()

    def memory_search(query: str, kind: str = "") -> str:
        """Search the team's shared memory (decisions, contracts, requirements, lessons,
        clarifications, handoffs). Optional kind filters to one record type."""
        kinds = [MemoryKind(kind)] if kind and kind in MemoryKind._value2member_map_ else None
        hits = engine.memory.recall(query, vis, kinds=kinds, limit=8)
        return "\n".join(render_index_line(h.record) for h in hits) or "(no matching memory)"

    def memory_read(record_id: str) -> str:
        """Read one shared-memory record in full by its id (e.g. MR-abc12345)."""
        rec = engine.memory.get(record_id)
        if rec is None or not vis.includes(rec.scope):
            return f"(no visible record {record_id})"
        return render_record(rec, max_chars=4_000)

    def memory_note(text: str) -> str:
        """Save a private working note for yourself (not shared with the team)."""
        scope = Scope.agent(reader.project_id, reader.agent_id) if reader.agent_id else reader.scope
        if scope is None:
            return "(no scope to write notes into)"
        res = engine.memory.remember(scope=scope, kind=MemoryKind.NOTE, title=first_line(text, 120), body=text,
                                     author=reader.agent_id or "agent", role=reader.role)
        return f"noted as {res.record.id}"

    return [StructuredTool.from_function(f) for f in (memory_search, memory_read, memory_note)]


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class LangChainTextLLM:
    """:class:`~aamt_context.llm.TextLLM` over any LangChain chat model."""

    def __init__(self, model: Any):
        self.model = model

    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        model = self.model
        try:
            model = model.bind(max_tokens=max_tokens)
        except Exception:  # noqa: BLE001 - not every model takes max_tokens
            pass
        resp = model.invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        return message_text(resp.content)


class LangChainChatModel:
    """:class:`~aamt_context.harness.ChatModel` over a LangChain chat model."""

    def __init__(self, model: Any):
        self.model = model

    def generate(self, entries: list[Entry], tools: list[ToolSpec]) -> ModelTurn:
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.parameters}} for t in tools]
        model = self.model.bind_tools(specs) if specs else self.model
        resp = model.invoke(messages_from_entries(entries))
        calls = [ToolCall(id=tc.get("id") or new_id("call"), name=tc["name"], args=tc.get("args") or {})
                 for tc in getattr(resp, "tool_calls", None) or []]
        usage = dict(getattr(resp, "usage_metadata", None) or {})
        return ModelTurn(content=message_text(resp.content), tool_calls=calls, usage=usage)


def tool_from_langchain(t: BaseTool) -> Tool:
    params = convert_to_openai_tool(t)["function"].get("parameters") or {"type": "object", "properties": {}}

    def run(**kwargs: Any) -> str:
        out = t.invoke(kwargs)
        return message_text(getattr(out, "content", out))

    return Tool(ToolSpec(name=t.name, description=t.description or "", parameters=params), run)
