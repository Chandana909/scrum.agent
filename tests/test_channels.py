from __future__ import annotations

import pytest

from aamt_context.channels import ChannelClosed, ChannelHub, Channels, render_message


def test_broadcast_is_delivered_to_every_reader_independently(store):
    """Regression for aamt's Mailbox: one agent draining a broadcast hid it from the rest."""
    hub = ChannelHub(store)
    ch = Channels.project("P1")
    hub.post(ch, "A-be", "API contract for /tasks changed", type="API_CONTRACT_UPDATE")
    fe = hub.unread("A-fe", [ch])
    assert [m.content for m in fe] == ["API contract for /tasks changed"]
    hub.ack("A-fe", fe)
    assert hub.unread("A-fe", [ch]) == []
    assert len(hub.unread("A-qa", [ch])) == 1          # still unread for QA
    assert hub.unread("A-be", [ch]) == []              # senders don't get their own messages


def test_direct_messages_and_addressing_by_alias(store):
    hub = ChannelHub(store)
    hub.send("A-be", "A-fe", "schema ready", type="DEPENDENCY_NOTICE", related=["T-2"])
    assert [m.content for m in hub.unread("A-fe", [Channels.inbox("A-fe")])] == ["schema ready"]
    assert hub.unread("A-qa", [Channels.inbox("A-fe")]) == []
    team = Channels.team("P1", "backend")
    hub.post(team, "scrum-master", "backend please review", recipients=["backend"])
    assert len(hub.unread("A-be", [team], aliases=["backend"])) == 1
    assert hub.unread("A-fe", [team], aliases=["frontend"]) == []


def test_type_filter_limit_and_ack_all(store):
    hub = ChannelHub(store)
    ch = Channels.project("P1")
    for i in range(3):
        hub.post(ch, "sm", f"q{i}", type="QUESTION")
    hub.post(ch, "sm", "fyi", type="MESSAGE")
    assert [m.content for m in hub.unread("A", [ch], types=["QUESTION"], limit=2)] == ["q0", "q1"]
    hub.ack_all("A", [ch])
    assert hub.unread("A", [ch]) == []


def test_closed_channels_reject_posts_and_rendering(store):
    hub = ChannelHub(store)
    ch = Channels.meeting("P1", "MT-1")
    msg = hub.post(ch, "A-fe", "hello")
    store.update_channel(ch, closed_at=1.0)
    with pytest.raises(ChannelClosed):
        hub.post(ch, "A-fe", "late")
    assert "A-fe -> *" in render_message(msg)
