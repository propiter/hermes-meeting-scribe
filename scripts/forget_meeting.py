#!/usr/bin/env python3
"""Erase a meeting that turned out to hold nothing, everywhere it left a trace.

A connection — a room open for seconds that held a few words — is not a meeting, and its notes are
noise in the channel. This removes the Discord messages it published, its folder, and every row that
points at it.

ORDER MATTERS. The Discord message ids live in the ``deliveries`` table: deleting the database rows
first would destroy the only record of which messages to remove, leaving them in the channel forever.

    python3 scripts/forget_meeting.py --data <plugin-data> <ids...>                 # dry run
    python3 scripts/forget_meeting.py --data <plugin-data> <ids...> --apply
    python3 scripts/forget_meeting.py --data <plugin-data> <ids...> --apply --local-only

``--local-only`` skips Discord, for a meeting that never published anything. The bot token is read
from ``DISCORD_BOT_TOKEN`` (the environment, else ``--env-file``); it is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_scribe.storage.repo import Repository  # noqa: E402

API = "https://discord.com/api/v10"


def _token(env_file: Path | None) -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token and env_file and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "DISCORD_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
                break
    return token


def _delete(path: str, token: str) -> tuple[bool, str]:
    # Discord answers 403 "error code: 1010" to a request with no User-Agent (its bot protection),
    # which reads exactly like a missing permission. Always send one.
    req = urllib.request.Request(f"{API}{path}", method="DELETE",
                                 headers={"Authorization": f"Bot {token}",
                                          "User-Agent": "DiscordBot (meeting-scribe, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status in (200, 204), str(response.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # already gone: nothing left to remove, which is the goal
            return True, "already gone (404)"
        return False, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:  # network, DNS, timeout: say so, never pretend it worked
        return False, f"{type(exc).__name__}: {exc}"


def targets(repo: Repository, meeting_id: str) -> tuple[list[str], list[tuple[str, str]]]:
    """``(thread ids, (channel, message) pairs)`` still to remove from Discord.

    A forum post is a THREAD whose id equals its starter message id, so a channel that also appears
    as a message id is deleted as a thread and takes everything inside with it. Nothing else is ever
    deleted as a channel — that is the guard that keeps this from touching a real channel.
    """
    rows = repo._x("SELECT external_id FROM deliveries WHERE meeting_id=?", (meeting_id,)).fetchall()
    channels: dict[str, list[str]] = {}
    for row in rows:
        try:
            ext = json.loads(row["external_id"] or "{}")
        except ValueError:
            continue
        channel = ext.get("channel")
        if channel is None:
            continue
        messages = list(ext.get("messages") or [])
        if ext.get("message"):
            messages.append(ext["message"])
        channels.setdefault(str(channel), []).extend(str(m) for m in messages)

    message_ids = {m for msgs in channels.values() for m in msgs}
    threads = [ch for ch in channels if ch in message_ids]
    pairs = [(ch, m) for ch, msgs in channels.items() if ch not in threads for m in msgs]
    return sorted(threads), sorted(pairs)


def forget(repo: Repository, data: Path, meeting_id: str, *, apply: bool, token: str,
           local_only: bool) -> bool:
    meeting = repo.get_meeting(meeting_id)
    if meeting is None:
        print(f"{meeting_id}: not in the index — nothing to do")
        return False
    threads, pairs = targets(repo, meeting_id)
    folder = data / meeting.folder if meeting.folder else None
    print(f"{meeting_id}: {meeting.title!r} ({meeting.state.value})")
    for thread in threads:
        print(f"    discord: delete thread {thread}")
    for channel, message in pairs:
        print(f"    discord: delete message {message} in {channel}")
    print(f"    local:   {'delete ' + str(folder) if folder and folder.is_dir() else 'no folder'}"
          f" + {len(Repository.MEETING_TABLES) + 1} tables")
    if not apply:
        return True

    if not local_only:
        if not token:
            raise SystemExit("DISCORD_BOT_TOKEN is not set (pass --env-file or --local-only)")
        for thread in threads:
            ok, detail = _delete(f"/channels/{thread}", token)
            print(f"    thread {thread}: {'deleted' if ok else 'FAILED ' + detail}")
            if not ok:
                raise SystemExit("stopping: the Discord copy is still there, and the ids that "
                                 "locate it live in the rows we would delete next")
        for channel, message in pairs:
            ok, detail = _delete(f"/channels/{channel}/messages/{message}", token)
            print(f"    message {message}: {'deleted' if ok else 'FAILED ' + detail}")
            if not ok:
                raise SystemExit("stopping: see above")
    if folder and folder.is_dir():
        shutil.rmtree(folder)
    repo.forget_meeting(meeting_id)
    print(f"    forgotten: {repo.get_meeting(meeting_id) is None}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("meeting_ids", nargs="+")
    ap.add_argument("--data", type=Path, required=True, help="the plugin's data directory")
    ap.add_argument("--env-file", type=Path, default=None, help="file holding DISCORD_BOT_TOKEN")
    ap.add_argument("--apply", action="store_true", help="do it (default: dry run)")
    ap.add_argument("--local-only", action="store_true", help="skip Discord")
    args = ap.parse_args(argv)

    if not args.data.is_dir():
        raise SystemExit(f"--data {args.data} is not a directory")
    repo = Repository(args.data / "index.sqlite")
    token = _token(args.env_file)
    done = [mid for mid in args.meeting_ids
            if forget(repo, args.data, mid, apply=args.apply, token=token,
                      local_only=args.local_only)]
    print(f"{len(done)} meeting(s)" + ("" if args.apply else " (dry run: nothing deleted)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
