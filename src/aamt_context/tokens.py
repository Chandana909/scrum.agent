"""Token counting.

aamt talks to OpenRouter models (Qwen, Mistral, ...) whose tokenizers we don't ship,
so the default is the same conservative chars/4 heuristic pi and AG2 use. Budgets are
ratios of the window, so a consistent over-estimate is safer than a precise count for
the wrong tokenizer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class HeuristicTokenCounter:
    chars_per_token: float = 4.0

    def count(self, text: str) -> int:
        if not text:
            return 0
        return math.ceil(len(text) / self.chars_per_token)


class TiktokenCounter:
    """Exact counts for OpenAI-family tokenizers (optional ``tiktoken`` extra)."""

    def __init__(self, encoding: str = "o200k_base"):
        import tiktoken

        self._enc = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._enc.encode(text, disallowed_special=()))


MESSAGE_OVERHEAD_TOKENS = 4


def default_counter() -> TokenCounter:
    return HeuristicTokenCounter()
