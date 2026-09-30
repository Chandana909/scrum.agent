"""The one LLM capability this package needs: text in, text out.

Summarisation and extraction are side jobs, so they go through this minimal protocol
instead of any framework's chat-model type. Every caller treats the LLM as optional:
on :class:`LLMUnavailable` or any exception it falls back to a deterministic path
(the same "LLM with deterministic fallback" pattern aamt uses for goals, standups and
retros).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


class LLMUnavailable(RuntimeError):
    """Raised by LLM adapters that have no backing model."""


@runtime_checkable
class TextLLM(Protocol):
    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str: ...


class FunctionLLM:
    """Adapt a plain ``fn(system, prompt, max_tokens) -> str``."""

    def __init__(self, fn: Callable[[str, str, int], str]):
        self._fn = fn

    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        return self._fn(system, prompt, max_tokens)


_FENCED = re.compile(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", re.DOTALL)
_BRACED = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def extract_json(text: str) -> Any | None:
    if not text:
        return None
    for rx in (_FENCED, _BRACED):
        m = rx.search(text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def complete_json(
    llm: TextLLM | None, system: str, prompt: str, *, max_tokens: int = 1024
) -> dict[str, Any] | None:
    """Best-effort JSON object from ``llm``; ``None`` on any failure or non-object reply."""
    if llm is None:
        return None
    try:
        data = extract_json(llm.complete(system, prompt, max_tokens=max_tokens))
    except Exception:  # noqa: BLE001 - callers fall back deterministically
        return None
    return data if isinstance(data, dict) else None


def complete_text(
    llm: TextLLM | None, system: str, prompt: str, *, max_tokens: int = 1024
) -> str | None:
    if llm is None:
        return None
    try:
        text = llm.complete(system, prompt, max_tokens=max_tokens)
    except Exception:  # noqa: BLE001
        return None
    return text.strip() or None
