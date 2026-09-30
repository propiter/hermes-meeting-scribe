"""Who a task belongs to, decided by a person after the meeting (DESIGN §16.2).

The same rules for every surface (the task card's buttons, the agent's tools, the CLI, the Desktop);
each surface only says WHO acts (:class:`Actor`) — it never widens the rules:

* taking an UNASSIGNED task for oneself ("I'll take it"): a participant of the meeting, or a member
  Hermes authorizes (``Actor.authorized``) who can see where the task lives (``Actor.sees``);
* a PRIVATE meeting (DESIGN §19.2) adds: only someone who can see its private channel (``Actor.sees``,
  checked by the surface), participants and owners included;
* giving a task to someone else, taking a task that already has an assignee, or undoing: owners
  (``Actor.admin``) and the local operator (CLI, Desktop: ``Actor.local``);
* releasing a task: its current assignee (only to nobody) or an owner;
* a direct-messages meeting (``:dm``, DESIGN §19.3): nobody from chat — only the local operator.

A change is pinned as an item override (it survives re-analysis), written into the notes, audited
(who, when, from whom to whom) and queued for Discord in :data:`ANNOUNCE_KV` (the card is edited in
place, the new assignee gets their DM panel and at most ONE mention). A task already in Linear gets
its assignee there too when the person maps to a Linear user; Kanban tasks have no person assignee.
Assigning a task to whoever already has it changes nothing (no audit, no Discord, no Linear call).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from .. import privacy
from ..domain.models import ActionItem, ActionStatus, Meeting, is_discord_user_id
from ..domain.names import match_person
from ..storage.artifacts import read_notes, write_notes

log = logging.getLogger(__name__)
SINK_NAMES = {"kanban": "Kanban", "linear": "Linear"}
ANNOUNCE_KV = "tasks.announce."  # + meeting id -> {item id: {"to", "from": [...], "actor"}}
NOBODY = frozenset({"none", "nobody", "unassigned", "nadie"})
ME = frozenset({"me", "yo"})
_CARD_KEY = re.compile(r"^mtg:(?P<meeting>[^:]+):(?:pdm:(?P<user>[0-9]+):)?task:(?P<item>.+)$")


class TaskAssignError(ValueError):
    """Why a task cannot be assigned (``code`` for the surfaces that translate)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Actor:
    """Who acts. ``user_id``: their Discord id (``cli``/``desktop`` for the local operator).
    ``admin``: an owner of the meeting's space. ``authorized``: Hermes lets this person use the bot
    (its user/role allowlist). ``sees``: the surface verified they can see where the task lives (the
    card's channel; for a private meeting its private channel). ``local``: the operator at this
    machine (CLI, Desktop), who may do anything. Surfaces fill these from facts they checked, never
    from what a person typed."""

    user_id: str
    admin: bool = False
    authorized: bool = False
    sees: bool = False
    local: bool = False
    name: str = ""

    @property
    def is_person(self) -> bool:
        return is_discord_user_id(self.user_id)


@dataclass(frozen=True)
class TaskAssigned:
    meeting_id: str
    item_id: str
    title: str
    previous: Optional[str]
    user: Optional[str]
    name: str
    changed: bool
    audit_id: Optional[int] = None
    sinks: dict[str, str] = field(default_factory=dict)  # sink -> synced | unmapped | cleared | unsupported | failed


@dataclass(frozen=True)
class Card:
    """A Discord message that shows one task: where it is, and whose direct-messages copy it is."""
    meeting_id: str
    item_id: str
    channel: str
    dm_of: str = ""  # the recipient, for a direct-messages copy (DESIGN §19.3)


def find_card(repo: Any, message_id: str) -> Optional[Card]:
    """The task shown by Discord message ``message_id`` (a reply to a task card names it)."""
    raw = str(message_id or "").strip()
    if not is_discord_user_id(raw):  # snowflakes, same shape as user ids
        return None
    for row in repo.find_task_cards(raw):
        m = _CARD_KEY.match(str(row["key"]))
        if m:
            ptr = json.loads(row["external_id"])
            return Card(m["meeting"], m["item"], str(ptr.get("channel") or ""), m["user"] or "")
    return None


def meeting_places(repo: Any, meeting_id: str) -> set[str]:
    """The Discord channels, threads and forum posts where the meeting's notes and task cards are (not
    the direct messages): someone chatting in one of them sees its tasks."""
    out: set[str] = set()
    prefix = f"mtg:{meeting_id}:"
    for row in repo.list_deliveries(meeting_id, sink="discord", prefix=prefix):
        suffix = str(row["key"])[len(prefix):]
        if suffix.startswith(("dm:", privacy.DM_COPY_PREFIX, "withdraw:", "pinged:")):
            continue
        try:
            ptr = json.loads(row.get("external_id") or "null")
        except ValueError:
            continue
        if isinstance(ptr, dict):
            out |= {str(ptr[k]) for k in ("channel", "thread", "forum") if ptr.get(k)}
    return out


def item_for_message(repo: Any, meeting_id: str, message_id: str) -> Optional[str]:
    card = find_card(repo, message_id)
    return card.item_id if card is not None and card.meeting_id == meeting_id else None


def participants(repo: Any, meeting: Meeting) -> list[str]:
    """The Discord ids of the people of the meeting (speakers, and Meet attendees an admin linked)."""
    return privacy.participants(repo, meeting)[0]


def _target(repo: Any, meeting: Meeting, who: str, actor: Actor, name: str) -> tuple[Optional[str], str]:
    """``(user id or None, display name)`` for ``who``: ``me``, nobody, a participant (id, ``<@id>``,
    ``@id`` or name) or any Discord user id (whether that person may get it is :func:`authorize`'s)."""
    raw = str(who or "").strip()
    if raw.lower() in NOBODY:
        return None, ""
    known = {s.user_id: s.name for s in meeting.human_speakers}
    known.update({pid: pname for pid, pname in privacy.people(repo, meeting) if pid})
    if raw.lower() in ME:
        if not actor.is_person:
            raise TaskAssignError("no_identity")
        return actor.user_id, name or known.get(actor.user_id) or actor.name or actor.user_id
    uid = raw.strip("<@!>") if raw.startswith(("@", "<@")) else raw
    if uid in known:
        return uid, name or known[uid] or uid
    key = match_person(raw.lstrip("@"), [(pid, (pname,)) for pid, pname in known.items()])
    if key is not None:
        return key, name or known[key] or key
    if is_discord_user_id(uid):
        link = repo.get_link(meeting.space, uid) if hasattr(repo, "get_link") else None
        return uid, name or str((link or {}).get("name") or "") or uid
    raise TaskAssignError("unknown_person", raw)


def authorize(repo: Any, meeting: Meeting, item: ActionItem, target: Optional[str], actor: Actor, *,
              private: bool, dm: bool) -> None:
    """Raise :class:`TaskAssignError` unless ``actor`` may give ``item`` to ``target`` (see module doc).
    ``private``/``dm``: the meeting is private / goes by direct messages only (DESIGN §19.2, §19.3)."""
    if actor.local:
        return
    if dm:
        raise TaskAssignError("dm_meeting")
    if not actor.is_person:
        raise TaskAssignError("no_identity")
    if private and not actor.sees:
        raise TaskAssignError("private_only")
    if actor.admin:
        return
    current = item.owner_speaker_id or None
    if target is None:
        if current == actor.user_id:
            return  # releasing one's own task
        raise TaskAssignError("owner_required" if current is None else "not_yours", current or "")
    if target != actor.user_id:
        raise TaskAssignError("self_only")
    if current is not None:
        raise TaskAssignError("taken", current)
    if actor.user_id in participants(repo, meeting) or (actor.authorized and actor.sees):
        return
    raise TaskAssignError("not_eligible")



def assign(service: Any, meeting_id: str, item_id: str, who: str, actor: Actor, *, name: str = "",
           sync: bool = True) -> TaskAssigned:
    from ..filelock import file_lock

    meeting = service.require(meeting_id)
    with file_lock(service.folder(meeting) / ".task-edit.lock"):
        return _assign(service, meeting, item_id, who, actor, name, sync)


def _assign(service: Any, meeting: Meeting, item_id: str, who: str, actor: Actor, name: str,
            sync: bool) -> TaskAssigned:
    repo = service.repo
    item = repo.get_action_item(meeting.id, item_id)
    if item is None:
        raise TaskAssignError("unknown_task", item_id)
    if item.status is ActionStatus.DISMISSED:
        raise TaskAssignError("dismissed", item.title)
    target, target_name = _target(repo, meeting, who, actor, name)
    current = item.owner_speaker_id or None
    if target == current:
        return TaskAssigned(meeting.id, item.id, item.title, current, target, item.owner_name or target_name, False)
    settings = service.settings(meeting.space)
    authorize(repo, meeting, item, target, actor, private=privacy.is_private(repo, settings, meeting),
              dm=privacy.is_dm(repo, settings, meeting))
    return _apply(service, meeting, item, target, target_name, actor.user_id, sync)


def _apply(service: Any, meeting: Meeting, item: ActionItem, target: Optional[str], target_name: str,
           actor_id: str, sync: bool) -> TaskAssigned:
    repo = service.repo
    current = item.owner_speaker_id or None
    updated = replace(item, owner_speaker_id=target, owner_name=target_name or None, owner_track_id=None)
    repo.set_owner_override(meeting.id, item.id, user=target, name=updated.owner_name)
    repo.update_action_item(meeting.id, updated)
    folder = service.folder(meeting)
    notes = read_notes(folder)
    if notes is not None:
        items = tuple(replace(updated, status=a.status, project=a.project, project_key=a.project_key)
                      if a.id == item.id else a for a in notes.action_items)
        write_notes(folder, meeting, replace(notes, action_items=items),
                    notes.language or service.settings(meeting.space).ui_language)
    audit_id = repo.audit_task(meeting.id, item.id, actor_id, current, target, service.clock.now().isoformat())
    _queue_announce(repo, meeting.id, item.id, current, target, actor_id)
    sinks = sync_sinks(service, meeting, repo.get_action_item(meeting.id, item.id) or updated) if sync else {}
    log.info("meeting-scribe %s: task %s assigned %s -> %s by %s", meeting.id, item.id, current, target, actor_id)
    return TaskAssigned(meeting.id, item.id, item.title, current, target, target_name, True, audit_id, sinks)


def _queue(repo: Any, meeting_id: str, item_id: str, build: Any) -> None:
    """Read-modify-write of the meeting's queue in ONE transaction (the assignment, a refresh and the
    sink's :func:`take_announcements` touch the same row from different threads). ``build(old record)``
    returns the new one; every write gets a higher ``seq``, so a record queued again while the sink is
    showing the previous one never equals it and is not dropped as already shown."""
    with repo.transaction():
        pending = pending_announcements(repo, meeting_id)
        seq = 1 + max((int(r.get("seq") or 0) for r in pending.values() if isinstance(r, dict)), default=0)
        pending[item_id] = {**build(pending.get(item_id) or {}), "seq": seq}
        repo.kv_set(ANNOUNCE_KV + meeting_id, json.dumps(pending, sort_keys=True))


def _queue_announce(repo: Any, meeting_id: str, item_id: str, previous: Optional[str], target: Optional[str],
                    actor_id: str) -> None:
    """What Discord still has to show for this change (read by the Discord sink, see module doc).
    ``from``: whose panels lost the task; ``ping``: the new assignee is someone else than who acted."""
    def build(rec: dict[str, Any]) -> dict[str, Any]:
        old = [u for u in [*rec.get("from", []), previous] if u and u != target]
        return {"to": target, "from": list(dict.fromkeys(old)), "actor": actor_id,
                "ping": bool(target) and target != actor_id}
    _queue(repo, meeting_id, item_id, build)


def queue_refresh(repo: Any, meeting_id: str, item_id: str) -> None:
    """Re-render a task's card in place (e.g. it went to Linear), pinging nobody."""
    item = repo.get_action_item(meeting_id, item_id)
    to = item.owner_speaker_id if item is not None else None
    _queue(repo, meeting_id, item_id,
           lambda rec: {**(rec or {"from": [], "ping": False, "actor": "", "refresh": True}), "to": to})


def take_announcements(repo: Any, meeting_id: str, done: dict[str, dict[str, Any]]) -> None:
    """Forget the announcements in ``done`` that did not change meanwhile (a newer one stays queued)."""
    with repo.transaction():
        pending = pending_announcements(repo, meeting_id)
        left = {k: v for k, v in pending.items() if done.get(k) != v}
        repo.kv_set(ANNOUNCE_KV + meeting_id, json.dumps(left, sort_keys=True) if left else None)


def meetings_to_announce(repo: Any) -> list[str]:
    return [key[len(ANNOUNCE_KV):] for key in repo.kv_prefix(ANNOUNCE_KV)]


def pending_announcements(repo: Any, meeting_id: str) -> dict[str, dict[str, Any]]:
    raw = repo.kv_get(ANNOUNCE_KV + meeting_id)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def sync_sinks(service: Any, meeting: Meeting, item: ActionItem) -> dict[str, str]:
    """Carry the new assignee to the external task already created for ``item`` (never creates one)."""
    out: dict[str, str] = {}
    for name, sink in service.item_sinks().items():
        if not repo_delivered(service.repo, meeting.id, item.id, name):
            continue
        set_assignee = getattr(sink, "set_assignee", None)
        if set_assignee is None:
            out[name] = "unsupported"
            continue
        try:
            out[name] = str(set_assignee(meeting, item))
        except Exception:  # an outage must not undo the assignment; reported to whoever asked
            log.exception("meeting-scribe %s: syncing the assignee of %s to %s failed", meeting.id, item.id, name)
            out[name] = "failed"
    return out


def repo_delivered(repo: Any, meeting_id: str, item_id: str, sink: str) -> bool:
    from ..domain.ids import idempotency_key

    return repo.get_delivery(sink, idempotency_key(meeting_id, item_id)) is not None


def undo(service: Any, meeting_id: str, item_id: str, actor: Actor) -> TaskAssigned:
    """Put back who had the task before its last assignment (owners and the local operator)."""
    from ..filelock import file_lock

    if not (actor.admin or actor.local):
        raise TaskAssignError("owner_required")
    meeting = service.require(meeting_id)
    with file_lock(service.folder(meeting) / ".task-edit.lock"):
        repo = service.repo
        item = repo.get_action_item(meeting.id, item_id)
        if item is None:
            raise TaskAssignError("unknown_task", item_id)
        settings = service.settings(meeting.space)
        if not actor.local and privacy.is_dm(repo, settings, meeting):
            raise TaskAssignError("dm_meeting")
        if not actor.local and not actor.sees and privacy.is_private(repo, settings, meeting):
            raise TaskAssignError("private_only")
        last = next((h for h in reversed(repo.task_history(meeting.id, item.id)) if not h["undone"]), None)
        if last is None:
            raise TaskAssignError("nothing_to_undo", item.title)
        previous = last["previous_user"] or None
        name = ""
        if previous is not None:
            name = next((n for u, n in privacy.people(repo, meeting) if u == previous), "") or previous
        done = _apply(service, meeting, item, previous, name, actor.user_id, True)
        repo.mark_task_audit_undone(int(last["id"]))
        return done


def assign_error(exc: TaskAssignError, lang: str) -> str:
    """Why a task could not be assigned, in the reader's words (mentions shown, never pinged)."""
    from ..i18n import t

    detail = f"<@{exc.detail}>" if exc.detail.isdigit() else exc.detail
    return t(f"assign.error_{exc.code}", lang, detail=detail)


def assign_reply(done: Any, lang: str, actor: str) -> str:
    """What the person who assigned reads: the change, and what happened in Linear/Kanban."""
    from ..i18n import t

    if not done.changed:
        text = t("assign.unchanged", lang, title=done.title)
    elif done.user is None:
        text = t("assign.released", lang, title=done.title)
    elif done.user == actor:
        text = t("assign.taken", lang, title=done.title)
    else:
        who = f"<@{done.user}>" if str(done.user).isdigit() else done.name
        text = t("assign.given", lang, title=done.title, user=who)
    for sink, status in sorted((done.sinks or {}).items()):
        text += "\n" + t(f"assign.sink_{status}", lang, sink=SINK_NAMES.get(sink, sink))
    return text
