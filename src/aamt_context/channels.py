"""Channels: the communication half of shared memory.

Messages are append-only and totally ordered (``seq``). Delivery state is a cursor per
(reader, channel), never a flag on the message — aamt's ``Mailbox`` marks a broadcast
``read`` for everyone as soon as one agent drains it; here each reader advances only
its own cursor.

Addressing inside a channel uses ``recipients``: ``"*"`` for everyone, or agent ids /
role aliases. Channel names are conventions, see :class:`Channels`.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from ._util import Clock, clip_chars, new_id
from .store import SqliteMemoryStore
from .types import BROADCAST, ChannelMessage


class ChannelClosed(RuntimeError):
    pass


class Channels:
    @staticmethod
    def inbox(agent_id: str) -> str:
        return f"inbox/{agent_id}"

    @staticmethod
    def project(project_id: str) -> str:
        return f"project/{project_id}"

    @staticmethod
    def team(project_id: str, team: str) -> str:
        return f"team/{project_id}/{team}"

    @staticmethod
    def meeting(project_id: str, meeting_id: str) -> str:
        return f"meeting/{project_id}/{meeting_id}"


class ChannelHub:
    def __init__(self, store: SqliteMemoryStore, *, clock: Clock | None = None):
        self.store = store
        self.clock = clock or store.clock

    def ensure(
        self, channel: str, *, kind: str | None = None, title: str = "",
        participants: Sequence[str] = (), meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.ensure_channel(
            channel, kind=kind or channel.split("/", 1)[0], title=title,
            participants=participants, meta=meta,
        )

    def post(
        self,
        channel: str,
        sender: str,
        content: str,
        *,
        type: str = "MESSAGE",
        recipients: Iterable[str] = (BROADCAST,),
        data: dict[str, Any] | None = None,
        related: Iterable[str] = (),
        reply_to: str | None = None,
        message_id: str | None = None,
    ) -> ChannelMessage:
        """Append a message. Pass a deterministic ``message_id`` (e.g. derived from the host
        event) to make re-posting after a crash or replay a no-op."""
        with self.store.transaction():   # the closed-check and the append are one step
            info = self.store.get_channel(channel) or self.ensure(channel)
            if info.get("closed_at"):
                raise ChannelClosed(f"channel {channel} is closed")
            msg = ChannelMessage(
                channel=channel, sender=sender, recipients=list(recipients) or [BROADCAST], type=type,
                content=content, data=dict(data or {}), related=list(related), reply_to=reply_to,
                ts=self.clock(), id=message_id or new_id("CM"),
            )
            return self.store.post_message(msg)

    def send(
        self, sender: str, recipient: str, content: str, *, type: str = "MESSAGE",
        data: dict[str, Any] | None = None, related: Iterable[str] = (), reply_to: str | None = None,
        message_id: str | None = None,
    ) -> ChannelMessage:
        """Direct message into ``recipient``'s inbox channel."""
        return self.post(
            Channels.inbox(recipient), sender, content, type=type, recipients=[recipient],
            data=data, related=related, reply_to=reply_to, message_id=message_id,
        )

    def history(self, channel: str, *, after_seq: int = 0, limit: int | None = None) -> list[ChannelMessage]:
        return self.store.messages(channel, after_seq=after_seq, limit=limit)

    @staticmethod
    def subscriptions(
        *, project_id: str, agent_id: str, team: str | None = None, meetings: Iterable[str] = (),
    ) -> list[str]:
        chans = [Channels.inbox(agent_id), Channels.project(project_id)]
        if team:
            chans.append(Channels.team(project_id, team))
        chans += list(meetings)
        return chans

    def unread(
        self,
        reader: str,
        channels: Iterable[str],
        *,
        aliases: Iterable[str] = (),
        types: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[ChannelMessage]:
        alias_set = {reader, *aliases}
        type_set = set(types) if types is not None else None
        out: list[ChannelMessage] = []
        for ch in channels:
            cursor = self.store.get_cursor(reader, ch)
            for m in self.store.messages(ch, after_seq=cursor):
                if m.sender == reader or not m.addressed_to(alias_set):
                    continue
                if type_set is not None and m.type not in type_set:
                    continue
                out.append(m)
        out.sort(key=lambda m: m.seq)
        return out[:limit] if limit else out

    def ack(self, reader: str, messages: Iterable[ChannelMessage]) -> None:
        """Advance ``reader``'s cursors past ``messages`` (per channel, forward only)."""
        top: dict[str, int] = {}
        for m in messages:
            top[m.channel] = max(top.get(m.channel, 0), m.seq)
        for ch, seq in top.items():
            self.store.set_cursor(reader, ch, seq)

    def ack_all(self, reader: str, channels: Iterable[str]) -> None:
        for ch in channels:
            latest = self.store.messages(ch, after_seq=self.store.get_cursor(reader, ch))
            if latest:
                self.store.set_cursor(reader, ch, latest[-1].seq)


def render_message(m: ChannelMessage, *, max_chars: int = 600, names: dict[str, str] | None = None) -> str:
    names = names or {}
    who = names.get(m.sender, m.sender)
    to = ",".join(names.get(r, r) for r in m.recipients)
    stamp = time.strftime("%H:%M:%S", time.localtime(m.ts))
    related = f" [{', '.join(m.related)}]" if m.related else ""
    return f"[{stamp}] {who} -> {to} ({m.type}){related}: {clip_chars(m.content.strip(), max_chars)}"
