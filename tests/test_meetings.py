from __future__ import annotations

import json
import re

import pytest

from aamt_context.channels import ChannelHub
from aamt_context.meetings import (
    MeetingClosed,
    MeetingKind,
    MeetingRoom,
    MeetingTimeUp,
    MinutesExtractor,
)
from aamt_context.types import MemoryKind, MemoryStatus, Scope

from .conftest import ScriptedLLM


@pytest.fixture
def room(memory, store, clock):
    return MeetingRoom(memory, ChannelHub(store), clock=clock)


def _open(room, **kw):
    return room.open("P1", kind=MeetingKind.REQUIREMENTS, title="Requirements & doubts",
                     participants=["A-fe", "A-be", "A-db"], agenda=["API shape", "Storage"], **kw)


def test_transcript_uses_timestamped_log_format(room, clock):
    m = _open(room)
    room.say(m, "A-fe", "API response structure needs clarification.")
    clock.advance(135)
    room.say(m, "A-be", "Response will follow the agreed schema.")
    text = room.transcript(m, names={"A-fe": "Frontend Agent", "A-be": "Backend Agent"})
    assert re.search(r'^\d\d:\d\d:\d\d - Frontend Agent:\n"API response structure needs clarification\."', text)
    assert "Backend Agent" in text


def test_time_box_is_enforced(room, clock):
    m = _open(room, time_box_s=60)
    room.say(m, "A-fe", "first")
    clock.advance(61)
    assert room.is_over(m)
    with pytest.raises(MeetingTimeUp):
        room.say(m, "A-be", "too late")
    room.say(m, "scrum-master", "wrap-up", allow_overtime=True)


def test_close_turns_markers_into_memory(room, memory, clock):
    prior = memory.remember(scope=Scope.project("P1"), kind=MemoryKind.DECISION, subject="api.transport",
                            title="Use gRPC everywhere", author="scrum-master").record
    m = _open(room)
    q = room.say(m, "A-fe", "QUESTION: What does GET /tasks return?")
    room.say(m, "A-be", "ANSWER: a JSON list of {id, title, done}", reply_to=q.id)
    room.say(m, "A-be", "CHANGE: api.transport -> Use REST for the public API because clients are browsers")
    room.say(m, "A-db", "DECISION: Use SQLite for the MVP because zero ops (subject: db.engine)\n"
                        "RISK: SQLite write locks under parallel tests")
    room.say(m, "scrum-master", "ACTION: Write the schema migration (owner: A-db)\n"
                                "QUESTION: Do we need soft deletes?\n"
                                "BLOCKER: T-abcd12 waits on the schema")
    out = room.close(m, sprint_id="S-1")

    assert [c.text for c in out.minutes.clarifications] == ["What does GET /tasks return?"]
    assert out.minutes.clarifications[0].answer == "a JSON list of {id, title, done}"
    assert [q.text for q in out.minutes.open_questions] == ["Do we need soft deletes?"]

    decisions = memory.store.query(kinds=[MemoryKind.DECISION])
    by_subject = {d.subject: d for d in decisions}
    assert by_subject["db.engine"].title == "Use SQLite for the MVP"
    assert by_subject["db.engine"].data["rationale"] == "zero ops"
    assert by_subject["api.transport"].title == "Use REST for the public API"
    assert memory.get(prior.id).status is MemoryStatus.SUPERSEDED     # the CHANGE retired it

    action = memory.store.query(kinds=[MemoryKind.ACTION_ITEM])[0]
    assert action.data["owner"] == "A-db" and action.title == "Write the schema migration"
    blocker = memory.store.query(kinds=[MemoryKind.BLOCKER])[0]
    assert "t-abcd12" in blocker.entities
    assert out.summary.record.kind is MemoryKind.SUMMARY and "Clarifications" in out.summary.record.body
    assert all(r.record.sprint_id == "S-1" for r in out.records)

    closed = room.get("P1", m.id)
    assert closed.ended_at is not None and closed.minutes_record_id == out.summary.record.id
    with pytest.raises(MeetingClosed):
        room.say(closed, "A-fe", "one more thing")


def test_llm_minutes_are_merged_with_markers(room, memory):
    llm = ScriptedLLM(json.dumps({
        "discussed": ["auth"],
        "decisions": [{"text": "Tokens expire after one hour", "by": "A-be"},
                      {"text": "Use SQLite for the MVP"}],          # duplicate of the marker line
        "action_items": ["Document the auth flow"],
    }))
    m = _open(room)
    room.say(m, "A-be", "I think an hour of token life is fine. Everyone agrees.")
    room.say(m, "A-db", "DECISION: Use SQLite for the MVP")
    out = room.close(m, extractor=MinutesExtractor(llm))
    texts = sorted(d.text for d in out.minutes.decisions)
    assert texts == ["Tokens expire after one hour", "Use SQLite for the MVP"]
    assert [a.text for a in out.minutes.action_items] == ["Document the auth flow"]
    assert "A-be" in llm.prompts[0][1]


def test_llm_failure_falls_back_to_markers(room):
    m = _open(room)
    room.say(m, "A-db", "decision: Use SQLite for the MVP")
    out = room.close(m, extractor=MinutesExtractor(ScriptedLLM(fail=True)))
    assert [d.text for d in out.minutes.decisions] == ["Use SQLite for the MVP"]


def test_participant_view_fits_budget(room):
    m = _open(room)
    for i in range(40):
        room.say(m, "A-fe", f"point number {i} " + "x" * 200)
    view = room.participant_view(m, max_tokens=800)
    assert "earlier messages omitted" in view
    assert "point number 39" in view and "point number 0 " not in view
    assert len(view) <= 800 * 4 + 200
