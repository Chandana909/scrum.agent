"""Meetings: time-boxed, timestamped conversations that end in minutes and memory.

Implements the memory side of the architecture doc's meeting model (§13-§15): every
utterance is logged with a timestamp in a meeting channel; when the meeting closes the
facilitator (Scrum Master) turns the transcript into structured minutes — decisions,
changes to earlier decisions, clarifications, open questions, action items, blockers,
dependencies, risks, assumptions — and writes each one into shared memory so "meetings
don't become chat that gets forgotten".

Turn-taking (who speaks when) belongs to the ceremony runner in the host; this module
only needs ``say()`` calls and a final ``close()``.

Extraction is two-layered: explicit line markers (``DECISION:``, ``ACTION:``,
``QUESTION:``/``ANSWER:``, ...) are parsed deterministically, and an optional LLM pass
adds items stated in free prose. Prompting agents to use the markers makes the
deterministic path sufficient on small models.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ._util import Clock, new_id, normalize_text
from .channels import ChannelHub, Channels
from .llm import TextLLM, complete_json
from .memory import SharedMemory
from .tokens import TokenCounter, default_counter
from .types import ChannelMessage, MemoryKind, Scope, WriteResult


class MeetingClosed(RuntimeError):
    pass


class MeetingTimeUp(RuntimeError):
    pass


class MeetingKind(str, Enum):
    REQUIREMENTS = "requirements"
    BRAINSTORM = "brainstorm"
    STANDUP = "standup"
    REVIEW = "review"
    RETROSPECTIVE = "retrospective"
    SME_REVIEW = "sme_review"
    ADHOC = "adhoc"


class Meeting(BaseModel):
    id: str
    project_id: str
    kind: MeetingKind
    title: str
    channel: str
    facilitator: str = "scrum-master"
    participants: list[str] = Field(default_factory=list)
    agenda: list[str] = Field(default_factory=list)
    started_at: float
    time_box_s: float = 900.0
    ended_at: float | None = None
    minutes_record_id: str | None = None

    @property
    def deadline(self) -> float:
        return self.started_at + self.time_box_s


class MinutesItem(BaseModel):
    text: str
    by: str | None = None
    rationale: str | None = None
    subject: str | None = None
    supersedes: str | None = None      # record id or subject key of the decision it changes
    owner: str | None = None
    answer: str | None = None
    answered_by: str | None = None
    related: list[str] = Field(default_factory=list)


_SECTIONS = (
    "decisions", "changes", "clarifications", "open_questions", "action_items", "blockers",
    "dependencies", "problems", "risks", "assumptions", "tasks",
)


class MeetingMinutes(BaseModel):
    discussed: list[str] = Field(default_factory=list)
    decisions: list[MinutesItem] = Field(default_factory=list)
    changes: list[MinutesItem] = Field(default_factory=list)
    clarifications: list[MinutesItem] = Field(default_factory=list)
    open_questions: list[MinutesItem] = Field(default_factory=list)
    action_items: list[MinutesItem] = Field(default_factory=list)
    blockers: list[MinutesItem] = Field(default_factory=list)
    dependencies: list[MinutesItem] = Field(default_factory=list)
    problems: list[MinutesItem] = Field(default_factory=list)
    risks: list[MinutesItem] = Field(default_factory=list)
    assumptions: list[MinutesItem] = Field(default_factory=list)
    tasks: list[MinutesItem] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(getattr(self, s) for s in _SECTIONS)

    def merge(self, other: MeetingMinutes | None) -> MeetingMinutes:
        if other is None:
            return self
        merged = self.model_copy(deep=True)
        for section in _SECTIONS:
            have = {normalize_text(i.text) for i in getattr(merged, section)}
            for item in getattr(other, section):
                if normalize_text(item.text) not in have:
                    getattr(merged, section).append(item)
                    have.add(normalize_text(item.text))
        for topic in other.discussed:
            if topic not in merged.discussed:
                merged.discussed.append(topic)
        return merged

    def render(self) -> str:
        titles = {
            "decisions": "Decisions", "changes": "Changes to previous decisions",
            "clarifications": "Clarifications", "open_questions": "Open questions",
            "action_items": "Action items", "blockers": "Blockers", "dependencies": "Dependencies",
            "problems": "Problems raised", "risks": "Risks", "assumptions": "Assumptions",
            "tasks": "Tasks created",
        }
        out: list[str] = []
        if self.discussed:
            out.append("### Discussed\n" + "\n".join(f"- {t}" for t in self.discussed))
        for section in _SECTIONS:
            items = getattr(self, section)
            if not items:
                continue
            lines = []
            for i in items:
                line = f"- {i.text}"
                if section == "clarifications" and i.answer:
                    line = f"- Q: {i.text}\n  A: {i.answer}"
                if i.rationale:
                    line += f" — because {i.rationale}"
                if i.owner:
                    line += f" (owner: {i.owner})"
                if i.supersedes:
                    line += f" (replaces {i.supersedes})"
                if i.by:
                    line += f" [{i.by}]"
                lines.append(line)
            out.append(f"### {titles[section]}\n" + "\n".join(lines))
        return "\n\n".join(out) or "_(nothing recorded)_"


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
# Deliberately few, unambiguous markers: "Task:" or "Issue:" appear in ordinary speech.
_MARKERS = {
    "DECISION": "decisions", "AGREED": "decisions",
    "CHANGE": "changes",
    "QUESTION": "open_questions",
    "ANSWER": "_answer",
    "ACTION": "action_items",
    "BLOCKER": "blockers",
    "DEPENDENCY": "dependencies",
    "PROBLEM": "problems",
    "RISK": "risks",
    "ASSUMPTION": "assumptions",
    "NEW TASK": "tasks",
}
_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(" + "|".join(re.escape(k) for k in _MARKERS) + r")\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_SUBJECT = re.compile(r"\((?:subject|re)\s*[:=]\s*([\w.\-/]+)\)", re.IGNORECASE)
_OWNER = re.compile(r"\((?:owner|assignee)\s*[:=]\s*([^)]+)\)", re.IGNORECASE)
_CHANGE = re.compile(r"^(\S+)\s*(?:->|:)\s*(.+)$")
_TASK_REF = re.compile(r"\bT-[a-z0-9]{4,}\b")


def _split_rationale(text: str) -> tuple[str, str | None]:
    m = re.search(r"\s+because\s+", text, re.IGNORECASE)
    if m and m.start() > 0:
        return text[: m.start()].strip(), text[m.end():].strip()
    return text, None


_LLM_SYSTEM = """You are a Scrum Master writing meeting minutes. From the transcript,
extract ONLY what was actually said. Reply with ONLY a JSON object with these keys,
each a list of objects {"text": str, "by": str|null, "rationale": str|null,
"subject": str|null, "supersedes": str|null, "owner": str|null, "answer": str|null}:
decisions, changes, clarifications, open_questions, action_items, blockers,
dependencies, problems, risks, assumptions, tasks. Also "discussed": [str].
- decisions: agreements the team reached (not proposals still being debated)
- changes: decisions that replace an earlier decision; put the earlier decision's
  subject or id in "supersedes"
- clarifications: questions that were answered (question in text, answer in answer)
- open_questions: questions nobody answered
Use short, self-contained sentences. Do not invent items."""


class MinutesExtractor:
    def __init__(self, llm: TextLLM | None = None, *, max_tokens: int = 1_500, max_transcript_chars: int = 24_000):
        self.llm = llm
        self.max_tokens = max_tokens
        self.max_transcript_chars = max_transcript_chars

    def extract(
        self, messages: Sequence[ChannelMessage], *, agenda: Sequence[str] = (),
        names: dict[str, str] | None = None,
    ) -> MeetingMinutes:
        minutes = self.deterministic(messages, agenda=agenda)
        if self.llm is not None:
            minutes = minutes.merge(self._with_llm(messages, agenda, names or {}))
        return minutes

    def deterministic(self, messages: Sequence[ChannelMessage], *, agenda: Sequence[str] = ()) -> MeetingMinutes:
        minutes = MeetingMinutes(discussed=list(agenda))
        open_by_msg: dict[str, MinutesItem] = {}
        for msg in messages:
            for raw in msg.content.splitlines():
                m = _LINE.match(raw)
                if not m:
                    continue
                marker, text = " ".join(m.group(1).upper().split()), m.group(2).strip()
                related = sorted(set(_TASK_REF.findall(text)) | set(msg.related))
                section = _MARKERS[marker]
                if section == "_answer":
                    target = open_by_msg.get(msg.reply_to or "") or (
                        minutes.open_questions[-1] if minutes.open_questions else None
                    )
                    if target is None:
                        continue
                    target.answer, target.answered_by = text, msg.sender
                    minutes.open_questions.remove(target)
                    minutes.clarifications.append(target)
                    open_by_msg = {k: v for k, v in open_by_msg.items() if v is not target}
                    continue
                item = MinutesItem(text=text, by=msg.sender, related=related)
                subject = _SUBJECT.search(text)
                if subject:
                    item.subject = subject.group(1)
                    item.text = _SUBJECT.sub("", item.text).strip()
                owner = _OWNER.search(item.text)
                if owner:
                    item.owner = owner.group(1).strip()
                    item.text = _OWNER.sub("", item.text).strip()
                if section == "changes":
                    cm = _CHANGE.match(item.text)
                    if cm:
                        item.supersedes, item.text = cm.group(1), cm.group(2).strip()
                if section in ("decisions", "changes"):
                    item.text, item.rationale = _split_rationale(item.text)
                getattr(minutes, section).append(item)
                if section == "open_questions":
                    open_by_msg[msg.id] = item
        return minutes

    def _with_llm(
        self, messages: Sequence[ChannelMessage], agenda: Sequence[str], names: dict[str, str]
    ) -> MeetingMinutes | None:
        transcript = render_transcript(messages, names=names)
        if len(transcript) > self.max_transcript_chars:
            half = self.max_transcript_chars // 2
            transcript = transcript[:half] + "\n…(middle of meeting omitted)…\n" + transcript[-half:]
        prompt = ("Agenda:\n" + "\n".join(f"- {a}" for a in agenda) + "\n\n" if agenda else "") + transcript
        data = complete_json(self.llm, _LLM_SYSTEM, prompt, max_tokens=self.max_tokens)
        if not data:
            return None
        cleaned: dict[str, Any] = {"discussed": [str(x) for x in data.get("discussed", []) if x]}
        for section in _SECTIONS:
            items = []
            for raw in data.get(section, []) or []:
                if isinstance(raw, str):
                    raw = {"text": raw}
                if isinstance(raw, dict) and raw.get("text"):
                    items.append({k: v for k, v in raw.items() if k in MinutesItem.model_fields and v is not None})
            cleaned[section] = items
        try:
            return MeetingMinutes.model_validate(cleaned)
        except Exception:  # noqa: BLE001
            return None


def render_transcript(
    messages: Iterable[ChannelMessage], *, names: dict[str, str] | None = None, fmt: str = "%H:%M:%S",
) -> str:
    """The architecture doc's log format: ``14:01:12 - Frontend Agent:`` + quoted text."""
    names = names or {}
    blocks = []
    for m in messages:
        stamp = time.strftime(fmt, time.localtime(m.ts))
        blocks.append(f'{stamp} - {names.get(m.sender, m.sender)}:\n"{m.content.strip()}"')
    return "\n\n".join(blocks)


@dataclass
class MeetingOutcome:
    meeting: Meeting
    minutes: MeetingMinutes
    records: list[WriteResult] = field(default_factory=list)
    summary: WriteResult | None = None


class MeetingRoom:
    def __init__(
        self,
        memory: SharedMemory,
        hub: ChannelHub,
        *,
        extractor: MinutesExtractor | None = None,
        counter: TokenCounter | None = None,
        clock: Clock | None = None,
    ):
        self.memory = memory
        self.hub = hub
        self.extractor = extractor or MinutesExtractor()
        self.counter = counter or default_counter()
        self.clock = clock or hub.clock

    # lifecycle -----------------------------------------------------------
    def open(
        self,
        project_id: str,
        *,
        kind: MeetingKind | str,
        title: str,
        participants: Sequence[str],
        facilitator: str = "scrum-master",
        agenda: Sequence[str] = (),
        time_box_s: float = 900.0,
        meeting_id: str | None = None,
    ) -> Meeting:
        mid = meeting_id or new_id("MT")
        meeting = Meeting(
            id=mid, project_id=project_id, kind=MeetingKind(kind), title=title,
            channel=Channels.meeting(project_id, mid), facilitator=facilitator,
            participants=list(participants), agenda=list(agenda), started_at=self.clock(),
            time_box_s=time_box_s,
        )
        self.hub.ensure(
            meeting.channel, kind="meeting", title=title,
            participants=[facilitator, *participants], meta={"meeting": meeting.model_dump(mode="json")},
        )
        return meeting

    def get(self, project_id: str, meeting_id: str) -> Meeting:
        info = self.hub.store.get_channel(Channels.meeting(project_id, meeting_id))
        if info is None:
            raise KeyError(meeting_id)
        return Meeting.model_validate(info["meta"]["meeting"])

    def time_left(self, meeting: Meeting) -> float:
        return max(0.0, meeting.deadline - self.clock())

    def is_over(self, meeting: Meeting) -> bool:
        return meeting.ended_at is not None or self.clock() >= meeting.deadline

    def say(
        self, meeting: Meeting, speaker: str, content: str, *, type: str = "UTTERANCE",
        reply_to: str | None = None, related: Iterable[str] = (), allow_overtime: bool = False,
    ) -> ChannelMessage:
        current = self.get(meeting.project_id, meeting.id)
        if current.ended_at is not None:
            raise MeetingClosed(meeting.id)
        if not allow_overtime and self.clock() >= current.deadline:
            raise MeetingTimeUp(f"{meeting.id}: time box of {current.time_box_s:.0f}s is over")
        return self.hub.post(meeting.channel, speaker, content, type=type, reply_to=reply_to, related=related)

    def messages(self, meeting: Meeting) -> list[ChannelMessage]:
        return self.hub.history(meeting.channel)

    def transcript(self, meeting: Meeting, *, names: dict[str, str] | None = None) -> str:
        return render_transcript(self.messages(meeting), names=names)

    def participant_view(
        self, meeting: Meeting, *, max_tokens: int = 3_000, names: dict[str, str] | None = None,
    ) -> str:
        """What a speaker sees: agenda, time left, and as much recent transcript as fits."""
        head = [f"# Meeting: {meeting.title} ({meeting.kind.value})"]
        if meeting.agenda:
            head.append("Agenda:\n" + "\n".join(f"- {a}" for a in meeting.agenda))
        head.append(f"Time left: {int(self.time_left(meeting))}s")
        header = "\n".join(head)
        budget = max_tokens - self.counter.count(header)
        msgs = self.messages(meeting)
        blocks: list[str] = []
        for m in reversed(msgs):
            block = render_transcript([m], names=names)
            cost = self.counter.count(block) + 2
            if cost > budget:
                break
            blocks.append(block)
            budget -= cost
        omitted = len(msgs) - len(blocks)
        body = "\n\n".join(reversed(blocks))
        if omitted:
            body = f"(…{omitted} earlier messages omitted…)\n\n" + body
        return f"{header}\n\n## Transcript so far\n{body or '(no one has spoken yet)'}"

    def close(
        self,
        meeting: Meeting,
        *,
        curator: str | None = None,
        sprint_id: str | None = None,
        extractor: MinutesExtractor | None = None,
        names: dict[str, str] | None = None,
    ) -> MeetingOutcome:
        current = self.get(meeting.project_id, meeting.id)
        if current.ended_at is not None:
            raise MeetingClosed(meeting.id)
        curator = curator or current.facilitator
        minutes = (extractor or self.extractor).extract(
            self.messages(current), agenda=current.agenda, names=names
        )
        records = self._persist(current, minutes, curator, sprint_id)
        summary = self.memory.remember(
            scope=Scope.project(current.project_id), kind=MemoryKind.SUMMARY, author=curator,
            title=f"Minutes: {current.title}", body=minutes.render(),
            tags=["meeting", current.kind.value], entities=[current.id], source_ref=current.id,
            source_key=f"minutes:{current.id}", sprint_id=sprint_id,
            data={"meeting": current.id, "kind": current.kind.value},
        )
        ended = current.model_copy(update={"ended_at": self.clock(), "minutes_record_id": summary.record.id})
        self.hub.store.update_channel(
            current.channel, meta={"meeting": ended.model_dump(mode="json")}, closed_at=ended.ended_at,
        )
        return MeetingOutcome(meeting=ended, minutes=minutes, records=records, summary=summary)

    # persistence ---------------------------------------------------------
    def _persist(
        self, meeting: Meeting, minutes: MeetingMinutes, curator: str, sprint_id: str | None,
    ) -> list[WriteResult]:
        scope = Scope.project(meeting.project_id)
        out: list[WriteResult] = []
        n = 0

        def write(kind: MemoryKind, item: MinutesItem, section: str, **extra: Any) -> None:
            nonlocal n
            n += 1
            data = {"meeting": meeting.id, "said_by": item.by, **extra.pop("data", {})}
            if item.rationale:
                data["rationale"] = item.rationale
            if item.owner:
                data["owner"] = item.owner
            out.append(self.memory.remember(
                scope=scope, kind=kind, author=curator, title=item.text,
                body=extra.pop("body", item.rationale and f"Rationale: {item.rationale}" or ""),
                entities=[*item.related, *( [item.owner] if item.owner else [])],
                source_ref=meeting.id, source_key=f"minutes:{meeting.id}:{section}:{n}",
                sprint_id=sprint_id, data=data, **extra,
            ))

        for item in minutes.decisions:
            write(MemoryKind.DECISION, item, "decision", **self._supersession(item))
        for item in minutes.changes:
            target = self._supersession(item) if item.supersedes else {}
            write(MemoryKind.DECISION, item, "change", data={"change": True}, **target)
        for item in minutes.clarifications:
            write(MemoryKind.CLARIFICATION, item, "clarification",
                  body=f"Q: {item.text}\nA: {item.answer}",
                  data={"question": item.text, "answer": item.answer, "asked_by": item.by,
                        "answered_by": item.answered_by, "open": False})
        for item in minutes.open_questions:
            write(MemoryKind.CLARIFICATION, item, "question",
                  data={"question": item.text, "asked_by": item.by, "open": True}, tags=["open-question"])
        for item in minutes.action_items:
            write(MemoryKind.ACTION_ITEM, item, "action")
        for item in minutes.tasks:
            write(MemoryKind.ACTION_ITEM, item, "task", tags=["task"])
        for item in minutes.blockers:
            write(MemoryKind.BLOCKER, item, "blocker")
        for item in minutes.dependencies:
            write(MemoryKind.FACT, item, "dependency", tags=["dependency"])
        for item in minutes.problems:
            write(MemoryKind.RISK, item, "problem", tags=["problem"])
        for item in minutes.risks:
            write(MemoryKind.RISK, item, "risk")
        for item in minutes.assumptions:
            write(MemoryKind.ASSUMPTION, item, "assumption")
        return out

    def _supersession(self, item: MinutesItem) -> dict[str, Any]:
        """Map ``supersedes``/``subject`` onto remember() kwargs."""
        target = item.supersedes
        if target and self.memory.get(target) is not None:
            return {"supersedes": target, "subject": item.subject}
        subject = item.subject or target
        return {"subject": subject} if subject else {}
