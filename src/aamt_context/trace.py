"""Deterministic facts pulled out of a transcript: files touched, tools used, errors,
test status. Shared by the extractive summariser and attempt memory, so both still
work when no LLM is available."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from ._util import clip_chars
from .worklog import Entry, EntryKind

_WRITE_HINTS = ("write", "edit", "create", "patch", "apply", "replace", "insert", "delete", "move", "rename")
_READ_HINTS = ("read", "view", "open", "cat", "list", "tree", "search", "grep", "find", "show")
_PATH_KEYS = ("path", "file_path", "filepath", "filename", "file", "target", "old_path", "new_path")

_ERR_LINE = re.compile(
    r"(Traceback \(most recent call last\)|\b[A-Za-z_]*(?:Error|Exception)\b|^FAILED\b|^ERROR\b|^E {2,}\S|exit=[1-9]\d*|TIMED_OUT|\bSyntaxError\b)"
)
_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+|\S+\.py)", re.M)


def _uniq(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in items if not (x in seen or seen.add(x))]


def file_ops(entries: Sequence[Entry]) -> tuple[list[str], list[str]]:
    """(files read, files modified) inferred from tool-call names and path arguments."""
    read: list[str] = []
    modified: list[str] = []
    for e in entries:
        for call in e.tool_calls:
            paths = [str(call.args[k]) for k in _PATH_KEYS if isinstance(call.args.get(k), str)]
            name = call.name.lower()
            if any(h in name for h in _WRITE_HINTS):
                modified += paths
            elif any(h in name for h in _READ_HINTS):
                read += paths
    modified = _uniq(modified)
    read = [p for p in _uniq(read) if p not in modified and p not in (".", "")]
    return read, modified


def tool_counts(entries: Sequence[Entry]) -> dict[str, int]:
    return dict(Counter(c.name for e in entries for c in e.tool_calls))


def error_lines(entries: Sequence[Entry], *, limit: int = 6) -> list[str]:
    found: list[str] = []
    for e in entries:
        if e.kind is not EntryKind.TOOL:
            continue
        for line in e.content.splitlines():
            if _ERR_LINE.search(line.strip()):
                found.append(clip_chars(line.strip(), 200))
    # most recent first, distinct
    return _uniq(list(reversed(found)))[:limit]


def _is_test_result(e: Entry) -> bool:
    name = (e.name or "").lower()
    return e.kind is EntryKind.TOOL and ("test" in name or e.content.lstrip().startswith("tests:"))


def last_test_status(entries: Sequence[Entry]) -> str | None:
    for e in reversed(entries):
        if _is_test_result(e):
            first = e.content.strip().splitlines()[0] if e.content.strip() else ""
            return clip_chars(first, 240) or None
    return None


def failing_tests(entries: Sequence[Entry], *, limit: int = 10) -> list[str]:
    for e in reversed(entries):
        if _is_test_result(e):
            return _uniq(_FAILED_TEST.findall(e.content))[:limit]
    return []


def last_assistant_texts(entries: Sequence[Entry], *, n: int = 2, chars: int = 400) -> list[str]:
    texts = [clip_chars(e.content.strip(), chars) for e in entries
             if e.kind is EntryKind.ASSISTANT and e.content.strip()]
    return texts[-n:]


def serialize(entries: Sequence[Entry], *, tool_chars: int = 1_500, text_chars: int = 3_000) -> str:
    """Flatten entries to labelled text so a summariser reads it as a record, not a chat."""
    out: list[str] = []
    for e in entries:
        if e.kind is EntryKind.SYSTEM:
            continue
        if e.kind is EntryKind.SUMMARY:
            out.append(f"[Earlier checkpoint]: {clip_chars(e.content, text_chars)}")
        elif e.kind is EntryKind.USER:
            out.append(f"[User]: {clip_chars(e.content, text_chars)}")
        elif e.kind is EntryKind.ASSISTANT:
            if e.content.strip():
                out.append(f"[Assistant]: {clip_chars(e.content, text_chars)}")
            if e.tool_calls:
                calls = "; ".join(
                    f"{c.name}(" + ", ".join(f"{k}={clip_chars(repr(v), 120)}" for k, v in c.args.items()) + ")"
                    for c in e.tool_calls
                )
                out.append(f"[Assistant tool calls]: {calls}")
        elif e.kind is EntryKind.TOOL:
            body = e.content
            if len(body) > tool_chars:
                body = body[: tool_chars // 2] + f"\n…[{len(body) - tool_chars} chars truncated]…\n" + body[-tool_chars // 2:]
            out.append(f"[Tool result {e.name or ''}]: {body}")
    return "\n".join(out)
