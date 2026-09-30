"""Restorable clipping of oversized tool output (Manus: "use the file system as context").

aamt's tools return up to 200 KB from ``read_file`` and ~20 KB from the test runner, all
of which lands in the transcript verbatim. Here any output above ``max_tokens`` keeps a
head and a tail (tail-heavy for commands and test runs, where the summary is at the end),
salvages error lines from the clipped middle, and parks the full text in the store under
a blob id that the agent can page through with ``read_stored_output``. Nothing is lost;
the transcript just holds a pointer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .store import SqliteMemoryStore
from .tokens import TokenCounter, default_counter

_SALVAGE = re.compile(
    r"(Traceback|\b[A-Za-z_]*(?:Error|Exception)\b|^FAILED\b|^ERROR\b|^E {2,}\S|\bassert\b)", re.IGNORECASE
)

DEFAULT_TAIL_HEAVY = ("run_test_suite", "run_command", "pytest", "bash", "shell", "terminal", "exec")


@dataclass
class ClipResult:
    text: str
    clipped: bool
    original_tokens: int
    blob_id: str | None = None


class ToolOutputClipper:
    def __init__(
        self,
        *,
        store: SqliteMemoryStore | None = None,
        counter: TokenCounter | None = None,
        max_tokens: int = 2_000,
        head_ratio: float = 0.6,
        tail_heavy_tools: tuple[str, ...] = DEFAULT_TAIL_HEAVY,
        salvage_lines: int = 8,
    ):
        self.store = store
        self.counter = counter or default_counter()
        self.max_tokens = max_tokens
        self.head_ratio = head_ratio
        self.tail_heavy_tools = tail_heavy_tools
        self.salvage_lines = salvage_lines

    def clip(self, tool_name: str, output: str, *, session_id: str | None = None) -> ClipResult:
        output = output if isinstance(output, str) else str(output)
        tokens = self.counter.count(output)
        if tokens <= self.max_tokens:
            return ClipResult(output, False, tokens)
        blob_id = (
            self.store.put_blob(output, session_id=session_id, meta={"tool": tool_name, "chars": len(output)})
            if self.store is not None else None
        )
        budget_chars = max(200, int(len(output) * self.max_tokens / tokens) - 300)
        ratio = 0.25 if any(t in tool_name.lower() for t in self.tail_heavy_tools) else self.head_ratio
        head_n = int(budget_chars * ratio)
        tail_n = budget_chars - head_n
        head, middle, tail = output[:head_n], output[head_n:len(output) - tail_n], output[len(output) - tail_n:]
        salvaged = [ln.strip() for ln in middle.splitlines() if _SALVAGE.search(ln.strip())][: self.salvage_lines]
        note = f"\n[… {len(middle)} of {len(output)} chars clipped"
        if blob_id:
            note += f"; full output stored — read_stored_output(blob_id=\"{blob_id}\", offset={head_n}) to see more"
        note += " …]\n"
        if salvaged:
            note += "[error lines from the clipped part:]\n" + "\n".join(salvaged) + "\n"
        return ClipResult(head + note + tail, True, tokens, blob_id)

    def read(self, blob_id: str, *, offset: int = 0, length: int = 6_000) -> str:
        if self.store is None:
            return "ERROR: no output store configured"
        blob = self.store.get_blob(blob_id)
        if blob is None:
            return f"ERROR: unknown blob_id {blob_id!r}"
        content, _meta = blob
        offset = max(0, min(offset, len(content)))
        end = min(len(content), offset + max(1, length))
        more = f" — call again with offset={end} for more" if end < len(content) else ""
        return f"{content[offset:end]}\n[chars {offset}-{end} of {len(content)}{more}]"
