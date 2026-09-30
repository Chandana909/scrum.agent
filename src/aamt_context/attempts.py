"""Attempt memory: what a failed attempt tried, so the retry doesn't start blind.

aamt's task graph retries with only the verifier's feedback; the previous attempt's
whole trajectory (files it explored, what it changed, which tests failed and how) is
thrown away and the next attempt re-explores from scratch. An :class:`AttemptSummary`
keeps the useful part — deterministically, no LLM needed — and is small enough to put
in the next attempt's brief ("keep the wrong stuff in", compressed).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from ._util import clip_chars
from .trace import error_lines, failing_tests, file_ops, last_assistant_texts, last_test_status, tool_counts
from .worklog import Entry


class AttemptSummary(BaseModel):
    attempt: int
    outcome: str                              # verify_failed | error | blocked | review_rejected | ...
    files_modified: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    tools: dict[str, int] = Field(default_factory=dict)
    test_status: str | None = None
    failing_tests: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    final_message: str = ""
    feedback: str = ""

    def title(self) -> str:
        return f"Attempt {self.attempt} {self.outcome}: " + (
            self.test_status or (self.errors[0] if self.errors else self.feedback.splitlines()[0] if self.feedback else "no details")
        )

    def render(self, *, max_chars: int = 1_800, include_feedback: bool = True) -> str:
        lines = [f"Attempt {self.attempt} — {self.outcome}"]
        if self.files_modified:
            lines.append(f"- changed: {', '.join(self.files_modified)}")
        if self.tools:
            lines.append("- tools: " + ", ".join(f"{k} x{v}" for k, v in sorted(self.tools.items())))
        if self.test_status:
            lines.append(f"- last test run: {self.test_status}")
        if self.failing_tests:
            lines.append(f"- failing: {', '.join(self.failing_tests)}")
        lines += [f"- error: {e}" for e in self.errors[:4]]
        if self.final_message:
            lines.append(f"- agent said: {clip_chars(self.final_message, 300)}")
        if self.feedback and include_feedback:
            lines.append(f"- verifier/reviewer: {clip_chars(self.feedback, 700)}")
        return clip_chars("\n".join(lines), max_chars)


def summarize_attempt(
    entries: Sequence[Entry], *, attempt: int, outcome: str, feedback: str = "",
) -> AttemptSummary:
    read, modified = file_ops(entries)
    final = last_assistant_texts(entries, n=1, chars=600)
    return AttemptSummary(
        attempt=attempt, outcome=outcome, files_modified=modified, files_read=read[:20],
        tools=tool_counts(entries), test_status=last_test_status(entries),
        failing_tests=failing_tests(entries), errors=error_lines(entries, limit=5),
        final_message=final[0] if final else "", feedback=feedback.strip(),
    )
