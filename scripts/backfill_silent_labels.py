#!/usr/bin/env python3
"""Drop the silent "unidentified-N" tracks from meetings processed before the fix.

A Discord connection that only streams silence still gets its own track: the receiver cannot know it
will never speak, so it labels the SSRC after ``UNIDENTIFIED_AFTER`` seconds and names the label
after its owner (or leaves it as "Participante sin identificar N"). Meetings captured before
``spoken_speakers`` landed in the transcribe stage carry those labels in their participant list —
repeating someone who already has a track of their own, and inventing a participant who never spoke.

Re-running the pipeline would re-transcribe hours of audio to redo a decision that is a pure function
of data already on disk, so this repairs the stored state instead. It uses the pipeline's own
``spoken_speakers`` and the repository's own writes, so it cannot drift from what the pipeline does.

A meeting lives in THREE places and all three must agree, or the next stage restores the labels:
``meetings.data`` (the JSON ``get_meeting`` returns), the ``speakers`` index table, and ``meta.json``.

    python3 scripts/backfill_silent_labels.py --data ~/.hermes/plugin-data/meeting-scribe abc1234
    python3 scripts/backfill_silent_labels.py --data ... abc1234 --apply

Without ``--apply`` it prints what it would drop and writes nothing. Run it with the interpreter that
has the plugin's dependencies (the Hermes venv), and with the plugin importable::

    PYTHONPATH=. ~/.hermes/hermes-agent/venv/bin/python scripts/backfill_silent_labels.py ...
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_scribe.pipeline.stages import spoken_speakers  # noqa: E402
from meeting_scribe.storage.artifacts import read_notes, read_transcript, write_meta, write_notes  # noqa: E402
from meeting_scribe.storage.repo import Repository  # noqa: E402

DEFAULT_DATA = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "plugin-data" / "meeting-scribe"


def folder_of(data: Path, meeting_id: str) -> Path:
    hits = [p for p in (data / "meetings").rglob(f"*_{meeting_id}") if p.is_dir()]
    if len(hits) != 1:
        raise SystemExit(f"{meeting_id}: expected exactly one folder, found {len(hits)}: {hits}")
    return hits[0]


def backfill(repo: Repository, data: Path, meeting_id: str, apply: bool) -> int:
    meeting = repo.get_meeting(meeting_id)
    if meeting is None:
        raise SystemExit(f"{meeting_id}: not in the index")
    folder = folder_of(data, meeting_id)
    kept = spoken_speakers(meeting.speakers, read_transcript(folder))
    kept_ids = {s.user_id for s in kept}
    dropped = [s for s in meeting.speakers if s.user_id not in kept_ids]

    print(f"{meeting_id}: {len(meeting.speakers)} -> {len(kept)} participants")
    for speaker in dropped:
        print(f"    dropping {speaker.user_id:20s} {speaker.name}")
    if not apply:
        return len(dropped)

    pruned = replace(meeting, speakers=kept)
    if dropped:
        for speaker in dropped:
            repo.delete_speaker(meeting_id, speaker.user_id)  # save_meeting upserts, it never removes
        repo.save_meeting(pruned)  # meetings.data + the index table
        write_meta(folder, pruned)  # meta.json
    # The notes already on disk name the participants: re-render them with the same call the analyze
    # stage makes, so the file matches the data without an LLM pass and without re-delivering.
    notes = read_notes(folder)
    if notes is not None:
        write_notes(folder, pruned, notes, notes.language or "es")
    return len(dropped)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("meeting_ids", nargs="+", help="meeting ids (or unique prefixes)")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help=f"the plugin's data directory (default: {DEFAULT_DATA})")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args(argv)

    if not args.data.is_dir():
        raise SystemExit(f"--data {args.data} is not a directory")
    repo = Repository(args.data / "index.sqlite")
    dropped = sum(backfill(repo, args.data, mid, args.apply) for mid in args.meeting_ids)
    print(f"{dropped} labels dropped" + ("" if args.apply else " (dry run: nothing written)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
