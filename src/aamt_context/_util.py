"""Small shared helpers: ids, clocks, text normalisation, hashing."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable, Iterable
from typing import Any

Clock = Callable[[], float]

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_id(prefix: str, n: int = 8) -> str:
    return f"{prefix}-" + "".join(secrets.choice(_ALPHABET) for _ in range(n))


def system_clock() -> float:
    return time.time()


_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(normalize_text(part).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from",
    "has", "have", "if", "in", "into", "is", "it", "its", "of", "on", "or", "should", "that",
    "the", "their", "then", "there", "these", "this", "to", "was", "we", "were", "will", "with",
    "you", "your", "must", "not", "no", "yes", "all", "any", "each", "when", "which", "who",
    "what", "how", "why", "also", "use", "using", "used", "via", "per",
])

_TERM = re.compile(r"[^\W_]+")   # letters and digits in any script


def terms(text: str, *, min_len: int = 2) -> list[str]:
    """Lowercase alphanumeric terms without stopwords, order-preserving and de-duplicated.

    Splits on everything else (including ``/`` ``.`` ``_``), which matches how SQLite's
    ``unicode61`` FTS tokenizer indexes the same text, in any script (``café``, ``日本``).
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TERM.findall((text or "").lower()):
        if len(tok) < min_len or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def normalize_entity(entity: str) -> str:
    return (entity or "").strip().replace("\\", "/").lower()


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)


def first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def clip_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|ts|tsx|js|jsx|md|toml|json|yaml|yml|sql|cfg|ini|txt|html|css))\b")


def find_paths(text: str) -> list[str]:
    """File-looking tokens (``src/api.py``, ``README.md``) mentioned in free text."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _PATH.finditer(text or ""):
        p = m.group(1).replace("\\", "/")
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
