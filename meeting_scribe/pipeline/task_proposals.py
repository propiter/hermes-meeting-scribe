"""Task changes the agent proposes in a SHARED conversation, run only when someone confirms them with a
Discord button (DESIGN §16.3).

In a conversation several people share (a Discord thread, or a channel without per-user sessions), the
Hermes session identity of a turn may be someone else's: a message from B that arrives while A's turn runs
is handled inside A's turn, with A's identity. So the agent's write tools never act on it there: they
store a proposal (what to do with which task, in which chat), post it with ✅ Confirm / ✖ Cancel and
return ``pending_confirmation``. The change is made when someone presses ✅ — AS that person, proved by
Discord's interaction, with exactly the task card's rules (``task_assign.authorize`` / ``auth.check_task``).

A proposal is used once (a compare-and-set on its state: two clicks never both run it), expires after
:data:`TTL_SECONDS`, survives a restart (SQLite ``task_proposals`` + persistent buttons) and keeps who
confirmed or cancelled it and what came of it. ``session_user`` is the identity the session had when it
was proposed: kept for the record, never used to authorize anything.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Optional

from ..i18n import t

TTL_SECONDS = 15 * 60
KINDS = frozenset({"assign", "send"})
PENDING, RUNNING, DONE, CANCELLED, EXPIRED = "pending", "running", "done", "cancelled", "expired"


@dataclass(frozen=True)
class Proposal:
    id: str
    meeting_id: str
    item_id: str
    kind: str  # assign | send
    arg: str  # assign: "me" (whoever confirms), "none" or a Discord user id; send: linear | kanban
    chat_id: str  # where it was posted: the only place it can be confirmed
    state: str
    expires_at: float
    message_id: str = ""
    session_user: str = ""
    decided_by: str = ""
    result: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Proposal":
        return cls(id=str(row["id"]), meeting_id=str(row["meeting_id"]), item_id=str(row["item_id"]),
                   kind=str(row["kind"]), arg=str(row["arg"]), chat_id=str(row["chat_id"]), state=str(row["state"]),
                   expires_at=float(row["expires_at"]), message_id=str(row.get("message_id") or ""),
                   session_user=str(row.get("session_user") or ""), decided_by=str(row.get("decided_by") or ""),
                   result=str(row.get("result") or ""))


def create(repo: Any, now: float, meeting_id: str, item_id: str, kind: str, arg: str, chat_id: str,
           session_user: str) -> Proposal:
    if kind not in KINDS:
        raise ValueError(f"unknown proposal kind {kind!r}")
    pid = secrets.token_hex(8)  # fits a button's custom_id (DESIGN §8)
    repo.add_task_proposal(pid, meeting_id, item_id, kind, arg, chat_id, session_user, now, now + TTL_SECONDS)
    return Proposal(pid, meeting_id, item_id, kind, arg, chat_id, PENDING, now + TTL_SECONDS,
                    session_user=session_user)


def get(repo: Any, pid: str) -> Optional[Proposal]:
    row = repo.get_task_proposal(pid)
    return Proposal.from_row(row) if row else None


def claim(repo: Any, pid: str, now: float) -> str:
    """Take a pending, unexpired proposal to run it: ``ok``, ``expired``, ``used`` or ``unknown``."""
    if repo.move_task_proposal(pid, frm=PENDING, to=RUNNING, now=now, unexpired=True):
        return "ok"
    current = get(repo, pid)
    if current is None:
        return "unknown"
    if current.state == PENDING and repo.move_task_proposal(pid, frm=PENDING, to=EXPIRED, now=now):
        return "expired"
    return "expired" if current.state == EXPIRED else "used"


def release(repo: Any, pid: str, now: float) -> None:
    """Back to pending after a refusal or an outage: someone else (an owner) may still confirm it."""
    repo.move_task_proposal(pid, frm=RUNNING, to=PENDING, now=now)


def finish(repo: Any, pid: str, now: float, by: str, result: str) -> None:
    repo.move_task_proposal(pid, frm=RUNNING, to=DONE, now=now, by=by, result=result)


def cancel(repo: Any, pid: str, now: float, by: str) -> bool:
    return repo.move_task_proposal(pid, frm=PENDING, to=CANCELLED, now=now, by=by)


def describe(kind: str, title: str, meeting_id: str, what: str, lang: str) -> str:
    """The proposal as posted in the chat: the task, the change, and that the one who confirms acts.
    ``title`` and ``what`` must already be inert text (``render.safe_name``): nobody is mentioned."""
    change = t(f"propose.{kind}", lang, what=what)
    return "\n".join((t("propose.header", lang, title=title, meeting=meeting_id), f"→ {change}",
                      t("propose.footer", lang, minutes=TTL_SECONDS // 60)))
