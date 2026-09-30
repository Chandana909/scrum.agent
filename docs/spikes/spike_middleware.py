"""Spike: can LangChain v1 + deepagents middleware replace aamt_context's in-run condensers?

Runs fully offline with scripted chat models. Checks, on a real `create_agent` graph:
  1. ContextEditingMiddleware clears old tool results in what the model sees
  2. deepagents SummarizationMiddleware summarises + offloads evicted history to a backend
  3. deepagents FilesystemMiddleware (read_file only) evicts oversized tool output to a file
  4. PIIMiddleware redacts tool results (built-in email + custom IBAN regex)

Run (any env with aamt's dependencies):
    uv pip install "deepagents==0.7.20"
    LANGSMITH_TRACING=false python docs/spikes/spike_middleware.py
Verified 2026-09-29 (langchain 1.4.3, deepagents 0.7.20, Windows / Python 3.11): 11 model calls,
4 summaries, full state kept (22 messages) while the model saw 11, 3 tool results cleared, test output
evicted to /large_tool_results/c100 with the FAILED line in the preview, email redacted, IBAN masked,
evicted history (14k chars) written to /conversation_history/<session>.md.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
)
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    PIIMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field


class Scripted(BaseChatModel):
    responses: list[AIMessage] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])

    def bind_tools(self, tools, **kwargs: Any):
        return self


class Summarizer(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "summarizer"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        text = f"## SESSION INTENT\nReview modules.\n## SUMMARY\nChecked m0..m{self.calls}.\n## NEXT STEPS\nContinue."
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


@tool
def read_module(path: str) -> str:
    """Read a source module."""
    return f"# {path}\n" + "def f():\n    return 1\n" * 120  # ~2.6k chars


@tool
def run_test_suite() -> str:
    """Run the tests."""
    return "\n".join(f"tests/test_{i}.py::test_case PASSED" for i in range(900)) + "\nFAILED tests/test_x.py::test_y - AssertionError"


@tool
def customer_lookup(cid: str) -> str:
    """Look up a customer record."""
    return f"customer {cid}: jane.doe@example.com IBAN GB33BUKB20201555555555 status=active"


def call(name: str, args: dict, i: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}", "type": "tool_call"}])


def main() -> None:
    root = Path(tempfile.mkdtemp())
    backend = FilesystemBackend(root_dir=str(root), virtual_mode=True)
    script = [call("read_module", {"path": f"src/m{i}.py"}, i) for i in range(8)]
    script += [call("run_test_suite", {}, 100), call("customer_lookup", {"cid": "42"}, 101)]
    script += [AIMessage(content="Done.")]
    model = Scripted(responses=script)
    summarizer = Summarizer()
    summ = SummarizationMiddleware(summarizer, backend=backend, trigger=("tokens", 3_500), keep=("messages", 6))
    agent = create_agent(
        model,
        tools=[read_module, run_test_suite, customer_lookup],
        middleware=[
            FilesystemMiddleware(backend=backend, tools=["read_file"], tool_token_limit_before_evict=1_500),
            summ,
            SummarizationToolMiddleware(summ),
            ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=2_000, keep=2)]),
            PIIMiddleware("email", strategy="redact", apply_to_input=False, apply_to_tool_results=True),
            PIIMiddleware("iban", detector=r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", strategy="mask",
                          apply_to_input=False, apply_to_tool_results=True),
        ],
        checkpointer=InMemorySaver(),
    )
    out = agent.invoke({"messages": [{"role": "user", "content": "Review every module, run tests, check customer 42."}]},
                       {"configurable": {"thread_id": "t1"}})

    last_view = model.seen[-1]
    tool_names = sorted({t for t in getattr(agent, "nodes", {})})
    print("graph nodes:", tool_names)
    print("model calls:", len(model.seen), "| summarizer calls:", summarizer.calls)
    print("messages in final state:", len(out["messages"]), "| in last model view:", len(last_view))
    summaries = [m for m in last_view if "SESSION INTENT" in str(m.content)]
    print("summary message visible to model:", bool(summaries), type(summaries[0]).__name__ if summaries else None)
    cleared = [m for m in last_view if isinstance(m, ToolMessage) and "[cleared]" in str(m.content)]
    print("cleared tool results in last view:", len(cleared))
    evicted = [m for m in out["messages"] if isinstance(m, ToolMessage) and "Tool result too large" in str(m.content)]
    print("evicted tool results:", len(evicted), "->", [ln for ln in str(evicted[0].content).splitlines() if "/large_tool_results" in ln][:1] if evicted else None)
    print("evicted preview keeps FAILED line:", any("FAILED tests/test_x.py" in str(m.content) for m in evicted))
    pii = [m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == "customer_lookup"]
    print("customer_lookup as model sees it:", pii[0].content if pii else None)
    files = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())
    print("backend files:", files)
    hist = [p for p in root.rglob("*.md") if "conversation_history" in str(p)]
    if hist:
        txt = hist[0].read_text(encoding="utf-8")
        print("offloaded history chars:", len(txt), "| contains src/m0.py:", "src/m0.py" in txt)


if __name__ == "__main__":
    main()
