"""A minimal agent loop that owns every context decision (optional).

This is the pi-style harness: model call -> tool calls -> results -> repeat, with the
context engine in the loop at each step instead of behind a framework hook:

* before every model call: run the condenser pipeline and build the view;
* on a provider context-overflow error: force a hard condensation and retry once;
* every tool result is clipped (restorable) before it enters the transcript;
* every step emits an event (for aamt's event bus / progress reporting);
* the loop is ~100 lines, framework-free, and testable with a scripted model.

It is not required for integration — :mod:`aamt_context.integrations.langchain` plugs
the same session into LangGraph's prebuilt agent — but it is the reference for what
"granular control" means (see docs/04-harness-and-langgraph.md).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, overload

from pydantic import BaseModel, Field

from ._util import new_id
from .session import ContextSession
from .worklog import Entry, ToolCall

logger = logging.getLogger(__name__)


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ModelTurn(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class ChatModel(Protocol):
    def generate(self, entries: list[Entry], tools: list[ToolSpec]) -> ModelTurn: ...


@dataclass
class Tool:
    spec: ToolSpec
    fn: Callable[..., Any]

    def __call__(self, **kwargs: Any) -> str:
        out = self.fn(**kwargs)
        return out if isinstance(out, str) else str(out)


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}
_JSON_TYPE_NAMES = {t.__name__: v for t, v in _JSON_TYPES.items()}


def _json_type(annotation: Any) -> str:
    # modules with `from __future__ import annotations` hand us strings ("int") rather than types
    if isinstance(annotation, str):
        return _JSON_TYPE_NAMES.get(annotation.strip(), "string")
    return _JSON_TYPES.get(annotation, "string")


@overload
def tool(fn: Callable[..., Any], *, name: str | None = None, description: str | None = None) -> Tool: ...


@overload
def tool(
    fn: None = None, *, name: str | None = None, description: str | None = None,
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    fn: Callable[..., Any] | None = None, *, name: str | None = None, description: str | None = None,
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Make a :class:`Tool` from a plain function with simple typed parameters."""

    def build(f: Callable[..., Any]) -> Tool:
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in inspect.signature(f).parameters.values():
            props[p.name] = {"type": _json_type(p.annotation)}
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
        spec = ToolSpec(
            name=name or f.__name__,
            description=description or (inspect.getdoc(f) or "").strip(),
            parameters={"type": "object", "properties": props, "required": required},
        )
        return Tool(spec, f)

    return build(fn) if fn is not None else build


_OVERFLOW = ("context length", "context_length", "maximum context", "too many tokens",
             "prompt is too long", "context window", "reduce the length")


def is_context_overflow(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _OVERFLOW)


class LoopEvent(BaseModel):
    step: int
    kind: str                       # condense | model | tool | stop
    detail: dict[str, Any] = Field(default_factory=dict)


@dataclass
class LoopResult:
    ok: bool
    final: str
    steps: int
    stop_reason: str                # final | max_steps | error
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    def __init__(
        self,
        model: ChatModel,
        tools: Sequence[Tool],
        session: ContextSession,
        *,
        max_steps: int = 40,
        on_event: Callable[[LoopEvent], None] | None = None,
        expose_stored_output: bool = True,
    ):
        self.model = model
        self.session = session
        self.max_steps = max_steps
        self.on_event = on_event
        self.tools = {t.spec.name: t for t in tools}
        if expose_stored_output and session.store is not None and "read_stored_output" not in self.tools:
            def read_stored_output(blob_id: str, offset: int = 0, length: int = 6_000) -> str:
                """Read more of a tool output that was clipped or cleared from your context."""
                return session.read_stored_output(blob_id, offset=offset, length=length)

            self.tools["read_stored_output"] = tool(read_stored_output)

    def _emit(self, step: int, kind: str, **detail: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event(LoopEvent(step=step, kind=kind, detail=detail))
            except Exception:  # observers must not break the loop
                logger.warning("loop event observer failed on %s", kind, exc_info=True)

    def run(self, user: str, *, system: str | None = None) -> LoopResult:
        if system:
            self.session.system(system)
        self.session.user(user)
        specs = [t.spec for t in self.tools.values()]
        calls_made: list[str] = []
        for step in range(1, self.max_steps + 1):
            applied = self.session.condense()
            if applied:
                self._emit(step, "condense", applied=applied, tokens=self.session.tokens())
            try:
                turn = self.model.generate(self.session.view(condense=False), specs)
            except Exception as exc:  # noqa: BLE001
                if not is_context_overflow(exc):
                    self._emit(step, "stop", reason="error", error=repr(exc))
                    return LoopResult(False, "", step, "error", calls_made, f"{type(exc).__name__}: {exc}",
                                      self.session.stats())
                self.session.condense(force=True)
                self._emit(step, "condense", applied=["overflow-recovery"], tokens=self.session.tokens())
                try:
                    turn = self.model.generate(self.session.view(condense=False), specs)
                except Exception as exc2:  # noqa: BLE001
                    return LoopResult(False, "", step, "error", calls_made, f"{type(exc2).__name__}: {exc2}",
                                      self.session.stats())
            calls = [c if c.id else c.model_copy(update={"id": new_id("call")}) for c in turn.tool_calls]
            self.session.assistant(turn.content, calls, meta={"usage": turn.usage} if turn.usage else {})
            self._emit(step, "model", tool_calls=[c.name for c in calls], usage=turn.usage)
            if not calls:
                self._emit(step, "stop", reason="final")
                return LoopResult(True, turn.content, step, "final", calls_made, None, self.session.stats())
            for call in calls:
                calls_made.append(call.name)
                t = self.tools.get(call.name)
                if t is None:
                    out = f"ERROR: unknown tool {call.name!r}. Available: {', '.join(sorted(self.tools))}"
                else:
                    try:
                        out = t(**call.args)
                    except Exception as exc:  # noqa: BLE001 - errors are observations
                        out = f"ERROR: {type(exc).__name__}: {exc}"
                entry = self.session.tool_result(call.id, call.name, out)
                self._emit(step, "tool", name=call.name, clipped=bool(entry.meta.get("clipped")))
        self._emit(self.max_steps, "stop", reason="max_steps")
        return LoopResult(False, "", self.max_steps, "max_steps", calls_made, "step budget exhausted",
                          self.session.stats())
