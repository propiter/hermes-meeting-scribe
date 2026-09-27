"""What a click on a notes button does (DESIGN §8), independent of discord.py.

Authorization:
  * Kanban (``ok``, ``allk``): owners only (``owners`` config → first ``DISCORD_ALLOWED_USERS``) —
    Kanban is the owner's personal task board.
  * Everything else (``lin``, ``alll``, ``no``, ``prj``, ``psel``): owners, or users Hermes itself
    authorizes for component clicks (``adapter._component_check_auth``: allowed users/roles,
    allow-all flags, pairing approvals).
Service calls hit SQLite/Kanban/Linear, so they run in a worker thread; the interaction is
deferred first (Discord's 3 s deadline) and answered with an ephemeral follow-up.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, Sequence

from ..config import Settings
from ..domain.models import Candidate
from ..i18n import t

log = logging.getLogger(__name__)
OWNER_ONLY = frozenset({"ok", "allk"})
SELECT_LIMIT = 25
REPLY_LIMIT = 1900  # Discord rejects messages over 2000 characters (review S2)


def clip_reply(text: str, limit: int = REPLY_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class ButtonActions:
    def __init__(self, *, service: Callable[[], Any], settings: Callable[[], Settings],
                 owners: Callable[[], Sequence[str]], check_auth: Callable[[Any], bool],
                 refresh: Callable[[str], Awaitable[None]],
                 project_view: Callable[[str, Sequence[Candidate]], Any]) -> None:
        self._service = service
        self._settings = settings
        self._owners = owners
        self._check_auth = check_auth
        self._refresh = refresh
        self._project_view = project_view

    @property
    def lang(self) -> str:
        return self._settings().ui_language

    def is_owner(self, interaction: Any) -> bool:
        return str(getattr(interaction.user, "id", "")) in {str(o) for o in self._owners()}

    def authorized(self, interaction: Any, action: str) -> bool:
        if self.is_owner(interaction):
            return True
        if action in OWNER_ONLY:
            return False
        try:
            return bool(self._check_auth(interaction))
        except Exception:  # fail closed
            log.exception("meeting-scribe: component auth check failed")
            return False

    async def handle(self, interaction: Any, action: str, meeting_id: str, item_id: str,
                     values: Optional[Sequence[str]] = None) -> None:
        if not self.authorized(interaction, action):
            key = "ui.owner_only" if action in OWNER_ONLY else "ui.not_allowed"
            await interaction.response.send_message(clip_reply(t(key, self.lang)), ephemeral=True)
            return
        if action == "prj":
            await self._offer_projects(interaction, meeting_id)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if values is None:
            values = list((getattr(interaction, "data", None) or {}).get("values") or ())
        try:
            reply = await asyncio.to_thread(self._run, action, meeting_id, item_id, list(values or ()))
        except (KeyError, LookupError, ValueError) as exc:
            reply = t("ui.action_failed", self.lang, error=str(exc).strip("'\""))
        except Exception as exc:  # Kanban/Linear outages: tell the clicker, keep the gateway healthy
            log.exception("meeting-scribe button %s failed", action)
            reply = t("ui.action_failed", self.lang, error=f"{type(exc).__name__}: {exc}")
        else:
            try:
                await self._refresh(meeting_id)
            except Exception:  # stale buttons are cosmetic; the action itself succeeded
                log.exception("meeting-scribe: refreshing notes of %s failed", meeting_id)
        await interaction.followup.send(clip_reply(reply), ephemeral=True)

    def _run(self, action: str, meeting_id: str, item_id: str, values: list[str]) -> str:
        svc = self._service()
        lang = self.lang
        if action in ("ok", "lin"):
            sink = "kanban" if action == "ok" else "linear"
            ref = svc.approve_item(meeting_id, item_id, sink)
            return t("ui.approved", lang, sink=sink, ref=ref)
        if action in ("allk", "alll"):
            sink = "kanban" if action == "allk" else "linear"
            res = svc.approve_all(meeting_id, sink)
            text = t("ui.approved_all", lang, sink=sink, count=len(res.delivered))
            return text + ("\n" + "\n".join(res.errors) if res.errors else "")
        if action == "no":
            svc.dismiss_item(meeting_id, item_id)
            return t("ui.dismissed", lang)
        if action == "psel":
            if not values:
                raise ValueError(t("ui.no_selection", lang))
            chosen = svc.set_project(meeting_id, values[0])
            return t("ui.project_saved", lang, project=chosen.name)
        raise ValueError(f"unknown action {action}")

    async def _offer_projects(self, interaction: Any, meeting_id: str) -> None:
        # Candidate lookup can hit Linear over HTTP: defer first, Discord's deadline is 3 s (S1).
        await interaction.response.defer(ephemeral=True, thinking=True)
        svc = self._service()
        try:
            meeting = await asyncio.to_thread(svc.require, meeting_id)
            cands = (await asyncio.to_thread(svc.candidates, meeting))[:SELECT_LIMIT]
        except Exception as exc:  # unknown meeting / catalog outage
            await interaction.followup.send(clip_reply(t("ui.action_failed", self.lang, error=str(exc))),
                                            ephemeral=True)
            return
        if not cands:
            await interaction.followup.send(t("ui.no_projects", self.lang), ephemeral=True)
            return
        await interaction.followup.send(t("ui.pick_project", self.lang), view=self._project_view(meeting_id, cands),
                                        ephemeral=True)
