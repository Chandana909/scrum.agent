"""Delegation contracts: what a parent hands a child, and what the child hands back.

Recursive orchestration (Scrum Master -> Backend Agent -> API sub-agent -> ...) fails
in two known ways: the child gets too little (Cognition's "implicit decisions" — two
sub-agents make incompatible choices) or too much (the parent's whole transcript). A
:class:`TaskBrief` is the explicit middle: objective, acceptance criteria, constraints,
what the child owns and must not touch, interfaces it must honour, and the upstream
work it builds on. The engine adds relevant shared memory around it.

A :class:`HandoffReport` is the bounded return value (Anthropic's multi-agent research
system has sub-agents return ~1-2k-token summaries and pass artifacts by reference).
Decisions, interfaces, assumptions and follow-ups the child reports become memory
*proposals* for the parent to review — bottom-up verification, not silent adoption.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from ._util import clip_chars, find_paths

DEFAULT_ESCALATION = [
    "the task needs a decision outside what you own",
    "the acceptance criteria conflict with a recorded decision or contract",
    "you depend on upstream work that is missing or broken",
]

REPORT_INSTRUCTIONS = """When you finish, end your final message with these lines (omit any that are empty):
SUMMARY: <one or two sentences: what you delivered>
FILES: <comma-separated paths you changed>
INTERFACE: <each interface/contract you created or changed, one per line>
DECISION: <each design decision you made, with "because ...", one per line>
ASSUMPTION: <each assumption you relied on, one per line>
QUESTION: <each open question for your lead, one per line>
FOLLOW-UP: <each piece of work you discovered but did not do, one per line>
RISK: <each risk you see, one per line>"""


class TaskBrief(BaseModel):
    objective: str
    task_id: str | None = None
    parent_task_id: str | None = None
    details: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    ownership: list[str] = Field(default_factory=list)       # paths/modules this agent may change
    interfaces: list[str] = Field(default_factory=list)      # contracts to honour, verbatim
    dependencies: list[str] = Field(default_factory=list)    # upstream task ids
    entities: list[str] = Field(default_factory=list)        # extra retrieval keys (files, components)
    escalate_when: list[str] = Field(default_factory=lambda: list(DEFAULT_ESCALATION))
    report_instructions: str | None = REPORT_INSTRUCTIONS
    max_steps: int | None = None

    def retrieval_query(self) -> str:
        return " ".join([self.objective, self.details, *self.acceptance_criteria, *self.deliverables])

    def retrieval_entities(self) -> list[str]:
        keys = [*self.entities, *self.dependencies, *self.ownership]
        if self.task_id:
            keys.append(self.task_id)
        keys += find_paths(f"{self.objective}\n{self.details}\n" + "\n".join(self.acceptance_criteria))
        seen: set[str] = set()
        return [k for k in keys if k and not (k in seen or seen.add(k))]

    def render_core(self) -> str:
        head = f"# Task {self.task_id}: {self.objective}" if self.task_id else f"# Task: {self.objective}"
        if self.parent_task_id:
            head += f"\n(sub-task of {self.parent_task_id})"
        parts = [head]
        if self.details.strip():
            parts.append(self.details.strip())
        blocks = [
            ("Acceptance criteria", self.acceptance_criteria),
            ("Deliverables", self.deliverables),
            ("Constraints", self.constraints),
            ("You own (change only these)", self.ownership),
            ("Interfaces you must honour", self.interfaces),
        ]
        for title, items in blocks:
            if items:
                parts.append(f"**{title}:**\n" + "\n".join(f"- {i}" for i in items))
        if self.max_steps:
            parts.append(f"**Budget:** at most {self.max_steps} tool calls.")
        if self.escalate_when:
            parts.append("**Stop and report a blocker if:**\n" + "\n".join(f"- {i}" for i in self.escalate_when))
        if self.report_instructions:
            parts.append(self.report_instructions)
        return "\n\n".join(parts)


def derive_child_brief(
    parent: TaskBrief,
    *,
    objective: str,
    task_id: str | None = None,
    details: str = "",
    acceptance_criteria: Sequence[str] = (),
    deliverables: Sequence[str] = (),
    ownership: Sequence[str] = (),
    interfaces: Sequence[str] = (),
    dependencies: Sequence[str] = (),
    max_steps: int | None = None,
) -> TaskBrief:
    """Narrow a parent's brief for a sub-agent.

    The child inherits the parent's constraints and interfaces (they bind the whole
    subtree), gets its own objective and criteria, and may only own a subset of what
    the parent owns.
    """
    if parent.ownership and ownership:
        outside = [o for o in ownership if not any(o == p or o.startswith(p.rstrip("/") + "/") for p in parent.ownership)]
        if outside:
            raise ValueError(f"child ownership {outside} is outside the parent's {parent.ownership}")
    return TaskBrief(
        objective=objective, task_id=task_id, parent_task_id=parent.task_id, details=details,
        acceptance_criteria=list(acceptance_criteria), deliverables=list(deliverables),
        constraints=list(parent.constraints), ownership=list(ownership or parent.ownership),
        interfaces=[*parent.interfaces, *interfaces], dependencies=list(dependencies),
        entities=list(parent.entities), escalate_when=list(parent.escalate_when),
        report_instructions=parent.report_instructions,
        max_steps=max_steps or (parent.max_steps // 2 if parent.max_steps else None),
    )


class HandoffReport(BaseModel):
    task_id: str
    agent: str
    outcome: str = "done"                     # done | partial | blocked | failed
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    def render(self, *, max_chars: int = 2_400) -> str:
        lines = [f"{self.task_id} ({self.outcome}, by {self.agent}): {self.summary or '(no summary)'}"]
        for label, items in (
            ("files", self.files_changed), ("interfaces", self.interfaces), ("decisions", self.decisions),
            ("assumptions", self.assumptions), ("open questions", self.open_questions),
            ("follow-ups", self.follow_ups), ("risks", self.risks), ("evidence", self.evidence),
            ("commits", [c[:10] for c in self.commits]),
        ):
            if items:
                lines.append(f"- {label}: " + "; ".join(items))
        return clip_chars("\n".join(lines), max_chars)

    @classmethod
    def from_text(
        cls, text: str, *, task_id: str, agent: str, outcome: str = "done",
        files_changed: Iterable[str] = (), commits: Iterable[str] = (), evidence: Iterable[str] = (),
    ) -> HandoffReport:
        """Parse the REPORT_INSTRUCTIONS markers; unmarked text becomes the summary."""
        found: dict[str, list[str]] = {k: [] for k in _REPORT_KEYS.values()}
        free: list[str] = []
        for line in (text or "").splitlines():
            m = _REPORT_LINE.match(line)
            if m:
                found[_REPORT_KEYS[m.group(1).upper().replace(" ", "-")]].append(m.group(2).strip())
            elif line.strip():
                free.append(line.strip())
        files = [f.strip() for chunk in found["files_changed"] for f in chunk.split(",") if f.strip()]
        summary = " ".join(found["summary"]) or clip_chars(" ".join(free), 600)
        return cls(
            task_id=task_id, agent=agent, outcome=outcome, summary=summary,
            files_changed=sorted({*files, *files_changed}), commits=list(commits),
            interfaces=found["interfaces"], decisions=found["decisions"], assumptions=found["assumptions"],
            open_questions=found["open_questions"], follow_ups=found["follow_ups"], risks=found["risks"],
            evidence=list(evidence),
        )


_REPORT_KEYS = {
    "SUMMARY": "summary", "FILES": "files_changed", "INTERFACE": "interfaces", "DECISION": "decisions",
    "ASSUMPTION": "assumptions", "QUESTION": "open_questions", "FOLLOW-UP": "follow_ups",
    "FOLLOWUP": "follow_ups", "RISK": "risks",
}
_REPORT_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(SUMMARY|FILES|INTERFACE|DECISION|ASSUMPTION|QUESTION|FOLLOW[- ]?UP|RISK)\s*:\s*(.+)$",
    re.IGNORECASE,
)
