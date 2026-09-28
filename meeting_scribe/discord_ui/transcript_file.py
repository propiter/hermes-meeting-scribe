"""Full transcript as a Discord attachment (DESIGN §17.3), for every meeting source.

Posted once per meeting, as its own message(s) right after the summary in the meeting chat:
``📎 Full transcript`` + ``transcript-<date>-<slug>.md`` (the same Markdown as ``transcript.md``).
Only the DELIVER stage attaches: button clicks and 📁 moves re-render the notes but never call
this. Idempotency lives in the ``transcript`` pointer (``deliveries``): a retry never re-attaches;
only a reprocess that CHANGED the transcript lines (sha256 of the lines, not of the title heading)
replaces the old message(s). Files above the size cap are split on line boundaries into numbered parts.

Crash safety: before each upload the pointer records ``sending`` (the file name). If the process
dies (or the pointer write fails) after Discord accepted the upload, the retry first looks for a
recent message of ours carrying that file name and adopts it instead of posting a duplicate.

Nothing here can fail a delivery: a missing *Attach Files* permission posts a one-line notice
(remembered, never retried in a loop); a transient error is logged and retried on the next publish.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..domain.ids import slugify
from ..domain.models import Meeting
from ..i18n import t
from .publisher import Messages, Pointers, is_missing
from .render import MessageSpec

log = logging.getLogger(__name__)
MAX_BYTES = 8 * 1024 * 1024  # conservative: the smallest per-file limit Discord has enforced
MAX_PARTS = 20
SUFFIX = "transcript"
HISTORY_LOOKBACK = 50  # recent messages searched for an upload whose pointer was never saved


@dataclass(frozen=True)
class TranscriptFile:
    name: str
    data: bytes


def base_name(meeting: Meeting) -> str:
    return f"transcript-{meeting.started_at:%Y-%m-%d}-{slugify(meeting.title or meeting.channel_name)}"


def split_parts(meeting: Meeting, text: str, *, max_bytes: int = MAX_BYTES) -> list[TranscriptFile]:
    """One file when it fits; else ``…-part1of3.md`` pieces cut at line ends (a giant line is hard-cut)."""
    data = text.encode("utf-8")
    base = base_name(meeting)
    if len(data) <= max_bytes:
        return [TranscriptFile(f"{base}.md", data)]
    chunks: list[bytes] = []
    cur = b""
    for line in text.splitlines(keepends=True):
        raw = line.encode("utf-8")
        while len(raw) > max_bytes:  # pathological single line
            head = raw[:max_bytes]
            while head and (raw[len(head)] & 0xC0) == 0x80:  # next byte continues a char: back off
                head = head[:-1]
            if cur:
                chunks.append(cur)
                cur = b""
            chunks.append(head)
            raw = raw[len(head):]
        if len(cur) + len(raw) > max_bytes:
            chunks.append(cur)
            cur = b""
        cur += raw
    if cur:
        chunks.append(cur)
    n = len(chunks)
    return [TranscriptFile(f"{base}-part{i}of{n}.md", c) for i, c in enumerate(chunks, start=1)]


def _no_permission(exc: BaseException) -> bool:
    """403 Missing Permissions (50013) / Missing Access (50001), or our fakes' PermissionError."""
    return isinstance(exc, PermissionError) or getattr(exc, "status", None) == 403 or \
        getattr(exc, "code", None) in (50001, 50013)


def content_digest(text: str) -> str:
    """sha256 of the transcript LINES: a new LLM title (the ``# …`` heading) is not a new transcript."""
    body = text.split("\n", 1)[1] if text.startswith("# ") and "\n" in text else text
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _bot_id(channel: Any, client: Any) -> Optional[int]:
    user_id = getattr(getattr(client, "user", None), "id", None)
    if isinstance(user_id, int):
        return user_id
    me = getattr(getattr(channel, "guild", None), "me", None)
    return getattr(me, "id", None)


async def find_upload(channel: Any, filename: str, client: Any = None) -> Optional[Any]:
    """A recent message of ours with attachment ``filename`` (``None`` when absent or unreadable)."""
    history = getattr(channel, "history", None)
    if not callable(history):
        return None
    bot = _bot_id(channel, client)
    try:
        async for msg in history(limit=HISTORY_LOOKBACK):
            if bot is not None and getattr(getattr(msg, "author", None), "id", None) != bot:
                continue
            if any(getattr(a, "filename", None) == filename for a in getattr(msg, "attachments", ()) or ()):
                return msg
    except Exception as exc:  # no Read Message History: fall back to posting
        log.info("meeting-scribe: cannot read history of %s to de-duplicate the transcript: %s",
                 getattr(channel, "id", "?"), exc)
    return None


async def publish_transcript(msgs: Messages, ptrs: Pointers, channel: Any, meeting: Meeting,
                             load_text: Callable[[Meeting], Optional[str]], make_file: Optional[Callable[..., Any]],
                             lang: str, *, max_bytes: int = MAX_BYTES) -> Optional[dict]:
    """Attach (once) the transcript in ``channel``; returns the pointer, ``None`` when nothing was done."""
    if make_file is None:
        return None
    text = await asyncio.to_thread(load_text, meeting)
    if not text or not text.strip():
        return None
    digest = content_digest(text)
    ptr = await ptrs.load(SUFFIX) or {}
    if ptr.get("skipped") == "legacy":
        return ptr  # delivered before attachments existed: never attached afterwards (privacy)
    if ptr.get("sha256") == digest and (ptr.get("done") or ptr.get("skipped")):
        return ptr
    if ptr.get("sha256") and ptr.get("sha256") != digest:  # reprocess changed it: replace the old one
        for mid in ptr.get("messages") or ():
            await msgs.delete(ptr.get("channel"), mid)
        ptr = {}
    parts = split_parts(meeting, text, max_bytes=max_bytes)
    ptr = {"sha256": digest, "channel": channel.id, "messages": list(ptr.get("messages") or ()),
           "parts": len(parts), "done": False, "sending": ptr.get("sending")}
    if len(parts) > MAX_PARTS:
        await _notice(msgs, channel, t("transcript.too_large", lang, parts=len(parts)))
        ptr.update(skipped="too_large", done=True)
        await ptrs.save(SUFFIX, ptr)
        return ptr
    client = getattr(msgs.adapter, "_client", None)
    for i, part in enumerate(parts[len(ptr["messages"]):], start=len(ptr["messages"]) + 1):
        label = t("transcript.attachment", lang) if len(parts) == 1 else \
            t("transcript.attachment_part", lang, n=i, total=len(parts))
        if ptr.get("sending") == part.name:  # an earlier upload's outcome is unknown: look before posting
            found = await find_upload(channel, part.name, client)
            if found is not None:
                ptr["messages"].append(found.id)
                ptr["sending"] = None
                await ptrs.save(SUFFIX, ptr)
                continue
        ptr["sending"] = part.name
        await ptrs.save(SUFFIX, ptr)  # BEFORE the upload: a lost pointer write can be healed
        try:
            msg = await channel.send(label, file=make_file(part.name, part.data), **msgs.views.mention_kwargs(()))
        except Exception as exc:
            if _no_permission(exc):
                log.info("meeting-scribe: cannot attach the transcript in %s: %s", channel.id, exc)
                await _notice(msgs, channel, t("transcript.no_permission", lang))
                ptr.update(skipped="no_permission", done=True, sending=None)
                await ptrs.save(SUFFIX, ptr)
                return ptr
            log.info("meeting-scribe: transcript attachment failed (%s); retrying on the next publish", exc)
            await ptrs.save(SUFFIX, ptr)  # parts already posted are kept: no duplicates on retry
            return ptr
        ptr["messages"].append(msg.id)
        ptr["sending"] = None
        await ptrs.save(SUFFIX, ptr)
    ptr["done"] = True
    ptr["sending"] = None
    await ptrs.save(SUFFIX, ptr)
    return ptr


async def mark_legacy(ptrs: Pointers) -> None:
    """A meeting whose summary predates transcript attachments: remember never to attach it."""
    await ptrs.save(SUFFIX, {"skipped": "legacy", "done": True})


async def _notice(msgs: Messages, channel: Any, text: str) -> None:
    try:
        await msgs.send(channel, spec=MessageSpec(text))
    except Exception as exc:
        if not is_missing(exc):
            log.info("meeting-scribe: transcript notice failed: %s", exc)
