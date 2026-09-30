"""Condensers: policies that shrink the working view by appending edits/condensations.

Ordered cheapest first, each with its own trigger:

1. :class:`ToolResultMasker` (no LLM) — above ``mask_trigger``, replace old tool
   outputs with one-line stubs that point at the stored full text, and elide large
   arguments of old tool calls (``write_file`` contents). Keeps the last N results.
   Clears down to a lower target (hysteresis) so the prompt prefix is not rewritten
   every turn — each rewrite invalidates the provider's prompt cache.
2. :class:`SummarizingCondenser` — above ``soft_limit`` (optional) or ``hard_limit``
   (required), forget the oldest turns after the protected prefix (system prompt +
   task brief), keep a verbatim tail of ``keep_recent_tokens``, and insert a structured
   checkpoint. Iterative: an earlier checkpoint inside the forgotten range is folded
   into the new one. Uses the LLM when present; on failure falls back to a
   deterministic checkpoint built from the trace, so a hard condensation never fails.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ._util import clip_chars
from .config import BudgetConfig
from .llm import TextLLM, complete_text
from .tokens import TokenCounter
from .trace import error_lines, failing_tests, file_ops, last_assistant_texts, last_test_status, serialize, tool_counts
from .worklog import (
    Condensation,
    ContextEdit,
    Entry,
    EntryKind,
    WorkingLog,
    cut_points,
    entry_tokens,
    view_tokens,
)

BlobSink = Callable[[str, str], str]  # (tool_name, content) -> blob id


@dataclass
class CondenseContext:
    counter: TokenCounter
    budget: BudgetConfig
    llm: TextLLM | None = None
    protected: int = 2          # leading view entries never forgotten (system + task brief)
    force: bool = False         # explicit request or context-overflow recovery
    focus: str | None = None    # extra summary instructions ("/compact <focus>")
    goal: str | None = None     # used by the deterministic checkpoint
    blob_sink: BlobSink | None = None


class Condenser(Protocol):
    name: str

    def condense(self, log: WorkingLog, ctx: CondenseContext) -> bool: ...


# ---------------------------------------------------------------------------
# 1. observation masking
# ---------------------------------------------------------------------------
@dataclass
class ToolResultMasker:
    exclude_tools: tuple[str, ...] = ()
    keep_last: int | None = None
    min_tokens: int | None = None
    target_ratio: float = 0.75
    elide_args_over: int = 800
    name: str = "mask"

    def condense(self, log: WorkingLog, ctx: CondenseContext) -> bool:
        view = log.view()
        total = view_tokens(view, ctx.counter)
        trigger = ctx.budget.mask_trigger
        if total <= trigger and not ctx.force:
            return False
        target = int(trigger * (0.5 if ctx.force else self.target_ratio))
        keep_last = ctx.budget.keep_last_tool_results if self.keep_last is None else self.keep_last
        min_tok = ctx.budget.min_maskable_tokens if self.min_tokens is None else self.min_tokens
        protected = {e.id for e in view[: ctx.protected]}
        changed = False

        tool_idx = [i for i, e in enumerate(view) if e.kind is EntryKind.TOOL]
        recent = set(tool_idx[-keep_last:]) if keep_last else set()
        for i in tool_idx:
            if total <= target:
                break
            e = view[i]
            if i in recent or e.id in protected or e.id in log.edits or (e.name or "") in self.exclude_tools:
                continue
            tok = ctx.counter.count(e.content)
            if tok < min_tok:
                continue
            blob = e.meta.get("blob_id")
            if blob is None and ctx.blob_sink is not None:
                blob = ctx.blob_sink(e.name or "tool", e.content)
            stub = f"[{e.name or 'tool'} output cleared from context ({tok} tokens)"
            stub += (f"; full text: read_stored_output(blob_id=\"{blob}\")]" if blob
                     else "; re-run the tool if you need it again]")
            log.add_edit(ContextEdit(target_id=e.id, replacement=stub, reason="mask"))
            total -= tok - ctx.counter.count(stub)
            changed = True

        asst_idx = [i for i, e in enumerate(view) if e.kind is EntryKind.ASSISTANT and e.tool_calls]
        recent_asst = set(asst_idx[-keep_last:]) if keep_last else set()
        for i in asst_idx:
            if total <= target:
                break
            e = view[i]
            if i in recent_asst or e.id in protected or e.id in log.edits:
                continue
            saved = 0
            calls = []
            for call in e.tool_calls:
                args = {}
                for k, v in call.args.items():
                    if isinstance(v, str) and len(v) > self.elide_args_over:
                        args[k] = f"[{len(v)} chars elided from context; this call already ran]"
                        saved += ctx.counter.count(v) - ctx.counter.count(args[k])
                    else:
                        args[k] = v
                calls.append(call.model_copy(update={"args": args}))
            if saved > 0:
                log.add_edit(ContextEdit(target_id=e.id, replacement_calls=calls, reason="elide_args"))
                total -= saved
                changed = True
        return changed


# ---------------------------------------------------------------------------
# 2. summarising
# ---------------------------------------------------------------------------
SUMMARY_SYSTEM = (
    "You are a context summarization assistant. You read the record of an AI software "
    "engineer's work session and write a structured checkpoint that lets the engineer "
    "continue seamlessly. Do NOT continue the work, answer questions, or give advice. "
    "Output ONLY the checkpoint."
)

CHECKPOINT_FORMAT = """Use this EXACT format:

## Goal
[What the engineer is trying to achieve, including the task id]

## Constraints & Decisions
- [Acceptance criteria, conventions and decisions that still apply, with reasons]

## Progress
### Done
- [x] [Completed steps, with file paths]
### In Progress
- [ ] [Current step]
### Blocked
- [Blockers, or "(none)"]

## Files
- Modified: [exact paths]
- Read: [exact paths worth re-reading]

## Tests & Errors
- [Latest test status line, failing test ids, exact error messages]

## Next Steps
1. [Ordered next actions]

## Critical Context
- [Anything else needed to continue: signatures, commands that worked, values]

Keep each section concise. Preserve exact file paths, function names, task ids,
commands and error messages."""

INITIAL_INSTRUCTIONS = "The record above is the part of the session being compacted. Write the checkpoint.\n\n" + CHECKPOINT_FORMAT
UPDATE_INSTRUCTIONS = (
    "The record above holds NEW session events. Update the checkpoint in <previous-checkpoint>: "
    "keep everything still relevant, move finished items to Done, update Next Steps, keep exact "
    "paths and errors, drop what is obsolete.\n\n" + CHECKPOINT_FORMAT
)

CHECKPOINT_HEADER = (
    "[Context checkpoint — earlier steps of this session were summarised to save space; "
    "the full record is preserved outside the context.]\n"
)


def extractive_checkpoint(entries: Sequence[Entry], *, previous: str | None, goal: str | None) -> str:
    """A checkpoint built only from the trace — the no-LLM fallback."""
    read, modified = file_ops(entries)
    counts = tool_counts(entries)
    parts = ["## Goal", clip_chars(goal or "(see the task brief above)", 500)]
    if previous:
        parts += ["## Earlier checkpoint", clip_chars(previous, 1_500)]
    parts += [
        "## Files",
        f"- Modified: {', '.join(modified) or '(none)'}",
        f"- Read: {', '.join(read[:25]) or '(none)'}",
        "## Tools used",
        "- " + (", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) or "(none)"),
    ]
    status = last_test_status(entries)
    failing = failing_tests(entries)
    errs = error_lines(entries)
    parts.append("## Tests & Errors")
    parts.append(f"- Latest test run: {status or '(no test run in this span)'}")
    if failing:
        parts.append("- Failing: " + ", ".join(failing))
    parts += [f"- {e}" for e in errs] or ["- (no errors seen)"]
    notes = last_assistant_texts(entries)
    if notes:
        parts.append("## Engineer's latest notes")
        parts += [f"- {n}" for n in notes]
    parts += ["## Next Steps", "1. Continue from the most recent state below; re-read modified files if needed."]
    return "\n".join(parts)


@dataclass
class SummarizingCondenser:
    llm: TextLLM | None = None            # defaults to the context's LLM
    keep_recent_tokens: int | None = None
    max_summary_tokens: int | None = None
    use_soft_trigger: bool = True
    name: str = "summarize"
    last_error: str | None = field(default=None, init=False)

    def condense(self, log: WorkingLog, ctx: CondenseContext) -> bool:
        view = log.view()
        total = view_tokens(view, ctx.counter)
        hard = ctx.force or total > ctx.budget.hard_limit
        soft = self.use_soft_trigger and total > ctx.budget.soft_limit
        if not (hard or soft):
            return False

        start = min(ctx.protected, len(view))
        keep_tail = ctx.budget.keep_recent_tokens if self.keep_recent_tokens is None else self.keep_recent_tokens
        tail_start, acc = len(view), 0
        for i in range(len(view) - 1, start - 1, -1):
            acc += entry_tokens(view[i], ctx.counter)
            if acc > keep_tail:
                break
            tail_start = i
        end = min((c for c in cut_points(view) if c >= max(tail_start, start + 1)), default=len(view))
        if end - start < 2 or end >= len(view) and not hard:
            if not hard:
                return False
            end = len(view)  # one giant turn: hard reset of everything after the prefix
        forgotten = view[start:end]
        if not forgotten:
            return False

        previous = "\n\n".join(e.content for e in forgotten if e.kind is EntryKind.SUMMARY) or None
        events = [e for e in forgotten if e.kind is not EntryKind.SUMMARY]
        summary, method = self._summarize(events, previous, ctx)
        after_id = view[start - 1].id if start > 0 else None
        cond = Condensation(
            forgotten_ids=[e.id for e in forgotten], summary=CHECKPOINT_HEADER + summary,
            after_id=after_id, reason="hard" if hard else "soft", method=method, tokens_before=total,
        )
        log.add_condensation(cond)
        cond.tokens_after = view_tokens(log.view(), ctx.counter)
        return True

    def _summarize(self, events: Sequence[Entry], previous: str | None, ctx: CondenseContext) -> tuple[str, str]:
        llm = self.llm or ctx.llm
        max_tokens = self.max_summary_tokens or ctx.budget.summary_max_tokens
        if llm is not None and events:
            prompt = f"<session-record>\n{serialize(events)}\n</session-record>\n\n"
            if previous:
                prompt += f"<previous-checkpoint>\n{previous}\n</previous-checkpoint>\n\n"
            if ctx.focus:
                prompt += f"Focus especially on: {ctx.focus}\n\n"
            prompt += UPDATE_INSTRUCTIONS if previous else INITIAL_INSTRUCTIONS
            text = complete_text(llm, SUMMARY_SYSTEM, prompt, max_tokens=max_tokens)
            if text and "##" in text:
                budget_chars = max_tokens * 4
                return clip_chars(text, budget_chars), "llm"
            self.last_error = "summary LLM returned nothing usable"
        return extractive_checkpoint(events, previous=previous, goal=ctx.goal), "extractive"


# ---------------------------------------------------------------------------
@dataclass
class CondenserPipeline:
    condensers: list[Condenser]

    def condense(self, log: WorkingLog, ctx: CondenseContext) -> list[str]:
        applied = []
        for c in self.condensers:
            if c.condense(log, ctx):
                applied.append(c.name)
        return applied


def default_pipeline(llm: TextLLM | None = None, *, exclude_tools: tuple[str, ...] = ()) -> CondenserPipeline:
    return CondenserPipeline([ToolResultMasker(exclude_tools=exclude_tools), SummarizingCondenser(llm=llm)])
