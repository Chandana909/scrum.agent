from __future__ import annotations

import pytest

from aamt_context.memory import SharedMemory
from aamt_context.store import SqliteMemoryStore


class FakeClock:
    def __init__(self, start: float = 1_760_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock):
    s = SqliteMemoryStore(":memory:", clock=clock)
    yield s
    s.close()


@pytest.fixture
def memory(store) -> SharedMemory:
    return SharedMemory(store)


class ScriptedLLM:
    """TextLLM that returns canned replies in order and records prompts."""

    def __init__(self, *replies: str, fail: bool = False):
        self.replies = list(replies)
        self.prompts: list[tuple[str, str]] = []
        self.fail = fail

    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        self.prompts.append((system, prompt))
        if self.fail:
            raise RuntimeError("model down")
        return self.replies.pop(0) if self.replies else ""
