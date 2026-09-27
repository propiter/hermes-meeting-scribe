"""Discord notes sink (DESIGN §8; satisfies :class:`meeting_scribe.sinks.base.DiscordNotesSink`).

``deliver`` runs on the pipeline thread: it renders synchronously (SQLite reads, candidate lookup),
then runs the publishing coroutine on the gateway loop with ``run_coroutine_threadsafe`` and waits
with a timeout. The result is a mutable pointer ``(sink="discord", key="mtg:<id>:notes")`` holding
``{"channel", "thread", "messages"}``: a reprocess (or a button click via :meth:`refresh`) edits
those messages in place, posts extras, deletes surplus, and re-posts only if they were deleted.

Target: ``delivery_discord_channel`` → the voice channel's text chat → Hermes home channel. The
header goes in the channel; with ``delivery_discord_thread`` the rest goes in a thread started from
it (voice text chats cannot host threads, so there everything stays in the channel).

Partial posts (review W8): the pointer is persisted after EVERY message sent, so when part N fails
(429, missing permission, timeout) the retry edits parts 1..N-1 in place and continues with the
rest instead of posting a duplicate header whose buttons would never be refreshed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from ..config import Settings
from ..domain.ids import idempotency_key
from ..domain.models import ActionItem, Meeting, Notes, SinkResult
from ..storage.artifacts import read_notes
from .render import ButtonSpec, MessageSpec, RenderOptions, render_notes

log = logging.getLogger(__name__)
SINK = "discord"


class ViewFactory(Protocol):
    def view(self, buttons: Sequence[ButtonSpec]) -> Any: ...

    def send_kwargs(self) -> dict[str, Any]: ...


class DiscordNotesSink:
    name = SINK

    def __init__(self, *, settings: Callable[[], Settings], service: Callable[[], Any], adapter: Callable[[], Any],
                 loop: Callable[[], Optional[asyncio.AbstractEventLoop]], options: Callable[[Meeting], RenderOptions],
                 views: ViewFactory, timeout: float = 120.0) -> None:
        self._settings = settings
        self._service = service
        self._adapter = adapter
        self._loop = loop
        self._options = options
        self._views = views
        self._timeout = timeout

    def enabled(self) -> bool:
        return self._settings().delivery_discord_enabled

    # -- pipeline thread -----------------------------------------------------------------------
    def render(self, meeting: Meeting, notes: Notes) -> list[MessageSpec]:
        stored = {a.id: a for a in self._service().repo.list_action_items(meeting.id)}
        items: list[ActionItem] = [stored.get(a.id, a) for a in notes.action_items]
        return render_notes(meeting, notes, items, self._options(meeting))

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        adapter, loop = self._adapter(), self._loop()
        if adapter is None or loop is None or loop.is_closed():
            return SinkResult(SINK, False, errors=("discord not connected yet; will retry",))
        specs = self.render(meeting, notes)
        fut = asyncio.run_coroutine_threadsafe(self.publish(meeting, specs), loop)
        try:
            url = fut.result(self._timeout)
        except Exception as exc:  # timeout, permissions, unknown channel: reported, the job retries
            fut.cancel()
            return SinkResult(SINK, False, errors=(f"{type(exc).__name__}: {exc}",))
        return SinkResult(SINK, True, (url,))

    # -- gateway loop ---------------------------------------------------------------------------
    async def refresh(self, meeting_id: str) -> None:
        """Re-render after a button action (status icons, remaining buttons) — runs on the loop."""
        svc = self._service()
        meeting = await asyncio.to_thread(svc.repo.get_meeting, meeting_id)
        if meeting is None:
            return
        notes = await asyncio.to_thread(read_notes, svc.folder(meeting))
        if notes is None:
            return
        specs = await asyncio.to_thread(self.render, meeting, notes)
        await self.publish(meeting, specs)

    def _targets(self, meeting: Meeting, adapter: Any) -> list[str]:
        home = getattr(getattr(getattr(adapter, "config", None), "home_channel", None), "chat_id", None)
        out: list[str] = []
        for cid in (self._settings().delivery_discord_channel, meeting.text_channel_id, meeting.channel_id, home):
            if cid and str(cid) not in out:
                out.append(str(cid))
        return out

    async def _resolve_target(self, meeting: Meeting, adapter: Any) -> Any:
        for cid in self._targets(meeting, adapter):
            try:
                return await adapter._resolve_channel(cid)
            except Exception as exc:  # deleted channel, missing access: try the next fallback
                log.info("meeting-scribe: notes channel %s unavailable: %s", cid, exc)
        raise LookupError("no reachable Discord channel for notes (delivery_discord_channel / voice chat / home)")

    async def publish(self, meeting: Meeting, specs: Sequence[MessageSpec]) -> str:
        adapter = self._adapter()
        if adapter is None:
            raise ConnectionError("discord not connected")
        key = idempotency_key(meeting.id, "notes")
        svc = self._service()
        row = await asyncio.to_thread(svc.repo.get_delivery, SINK, key)
        pointer = json.loads(row["external_id"]) if row and row.get("external_id") else None

        async def save(ptr: dict, url: str) -> None:
            await asyncio.to_thread(svc.repo.upsert_delivery, meeting.id, SINK, key,
                                    external_id=json.dumps(ptr), url=url)

        result = await self._edit(adapter, pointer, specs, save) if pointer else None
        if result is None:
            result = await self._post(meeting, adapter, specs, save)
        ptr, url = result
        await save(ptr, url)
        return url

    async def _post(self, meeting: Meeting, adapter: Any, specs: Sequence[MessageSpec],
                    save: Callable[[dict, str], Any]) -> tuple[dict, str]:
        channel = await self._resolve_target(meeting, adapter)
        first = await channel.send(specs[0].content, view=self._views.view(specs[0].buttons),
                                   **self._views.send_kwargs())
        await save({"channel": channel.id, "thread": None, "messages": [first.id]}, first.jump_url)
        target, thread_id = channel, None
        if len(specs) > 1 and self._settings().delivery_discord_thread:
            try:
                title = (meeting.title or meeting.channel_name or "meeting")[:90]
                thread = await first.create_thread(name=f"🎙️ {title}", auto_archive_duration=1440)
                target, thread_id = thread, thread.id
                await save({"channel": channel.id, "thread": thread_id, "messages": [first.id]}, first.jump_url)
            except Exception as exc:  # voice text chats / missing Create Public Threads: stay in channel
                log.info("meeting-scribe: no thread for notes (%s); posting in channel", exc)
        ids = [first.id]
        for spec in specs[1:]:
            msg = await target.send(spec.content, view=self._views.view(spec.buttons), **self._views.send_kwargs())
            ids.append(msg.id)
            await save({"channel": channel.id, "thread": thread_id, "messages": list(ids)}, first.jump_url)
        return {"channel": channel.id, "thread": thread_id, "messages": ids}, first.jump_url

    async def _edit(self, adapter: Any, ptr: dict, specs: Sequence[MessageSpec],
                    save: Callable[[dict, str], Any]) -> Optional[tuple[dict, str]]:
        """Edit the stored messages in place; ``None`` means "gone, post again"."""
        try:
            channel = await adapter._resolve_channel(ptr["channel"])
            thread = await adapter._resolve_channel(ptr["thread"]) if ptr.get("thread") else channel
            ids: list[int] = list(ptr.get("messages") or [])
            if not ids:
                return None
            first = channel.get_partial_message(ids[0])
            await first.edit(content=specs[0].content, view=self._views.view(specs[0].buttons))
        except Exception as exc:  # message/channel deleted: publish a fresh copy
            log.info("meeting-scribe: stored notes message unavailable (%s); re-posting", exc)
            return None
        kept = [ids[0]]
        url = getattr(first, "jump_url", "") or ""
        for i, spec in enumerate(specs[1:], start=1):
            view = self._views.view(spec.buttons)
            if i < len(ids):
                try:
                    await thread.get_partial_message(ids[i]).edit(content=spec.content, view=view)
                    kept.append(ids[i])
                    continue
                except Exception as exc:
                    log.info("meeting-scribe: notes part %s gone (%s); sending a new one", ids[i], exc)
            msg = await thread.send(spec.content, view=view, **self._views.send_kwargs())
            kept.append(msg.id)
            await save({"channel": ptr["channel"], "thread": ptr.get("thread"),
                        "messages": kept + ids[i + 1:]}, url)
        for surplus in ids[len(specs):]:
            try:
                await thread.get_partial_message(surplus).delete()
            except Exception as exc:
                log.info("meeting-scribe: could not delete surplus notes message %s: %s", surplus, exc)
        return {"channel": ptr["channel"], "thread": ptr.get("thread"), "messages": kept}, url
