"""Meeting folder artifacts (DESIGN §9). Every write is atomic (tmp + ``os.replace``) so a crash
never leaves a half-written ``notes.md`` that a later reprocess or the Obsidian sink would copy."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from ..domain.models import Meeting, Notes, Utterance
from ..i18n import t

META, TRANSCRIPT, TRANSCRIPT_MD = "meta.json", "transcript.jsonl", "transcript.md"
NOTES_MD, NOTES_JSON, TASKS_JSON = "notes.md", "notes.json", "tasks.json"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def fmt_ts(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    return f"{h:d}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60:02d}:{rem % 60:02d}"


# -- meta ---------------------------------------------------------------------------------------
def write_meta(folder: Path, meeting: Meeting) -> None:
    atomic_write_text(folder / META, _dump(meeting.to_dict()))


def read_meta(folder: Path) -> Optional[Meeting]:
    p = folder / META
    return Meeting.from_dict(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None


# -- transcript ---------------------------------------------------------------------------------
def write_transcript(folder: Path, utterances: Iterable[Utterance]) -> None:
    ordered = sorted(utterances, key=lambda u: (u.t0, u.speaker_id))
    atomic_write_text(folder / TRANSCRIPT,
                      "".join(json.dumps(u.to_dict(), ensure_ascii=False) + "\n" for u in ordered))


def read_transcript(folder: Path) -> list[Utterance]:
    p = folder / TRANSCRIPT
    if not p.exists():
        return []
    return [Utterance.from_dict(json.loads(line)) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def render_transcript_md(meeting: Meeting, utterances: Iterable[Utterance], lang: str) -> str:
    lines = [f"# {t('transcript.title', lang, title=meeting.title or meeting.channel_name)}", ""]
    lines += [f"**[{fmt_ts(u.t0)}] {u.speaker}:** {u.text}" for u in utterances]
    return "\n".join(lines) + "\n"


def write_transcript_md(folder: Path, meeting: Meeting, utterances: Iterable[Utterance], lang: str) -> None:
    atomic_write_text(folder / TRANSCRIPT_MD, render_transcript_md(meeting, utterances, lang))


# -- notes --------------------------------------------------------------------------------------
def _frontmatter(meeting: Meeting, notes: Notes) -> str:
    """YAML frontmatter using JSON scalars (valid YAML, no dependency, injection-proof)."""
    fields: dict[str, Any] = {
        "title": notes.meeting_title or meeting.title or meeting.channel_name,
        "date": meeting.started_at.date().isoformat(),
        "start": meeting.started_at.isoformat(),
        "end": meeting.ended_at.isoformat() if meeting.ended_at else None,
        "meeting_id": meeting.id,
        "guild": meeting.guild_name or meeting.guild_id,
        "channel": meeting.channel_name,
        "participants": [s.name for s in meeting.human_speakers],
        "project": notes.project,
        "tags": ["meeting", "meeting-scribe"],
    }
    body = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items())
    return f"---\n{body}\n---\n"


def one_line(text: str) -> str:
    """Collapse whitespace: LLM-provided titles must not inject Markdown structure (``---``, ``#``)."""
    return " ".join(str(text).split())


def render_notes_md(meeting: Meeting, notes: Notes, lang: str) -> str:
    none = t("notes.none", lang)
    title = one_line(notes.meeting_title or meeting.title or meeting.channel_name)
    out = [_frontmatter(meeting, notes), f"# {title}", ""]
    if meeting.partial:
        out += [f"> {t('notes.partial', lang)}", ""]
    out += [f"**{t('notes.tldr', lang)}:** {notes.tldr}", "", f"## {t('notes.summary', lang)}", "",
            notes.summary or none, ""]
    if notes.topics:
        out += [f"## {t('notes.topics', lang)}", ""]
        for topic in notes.topics:
            out += [f"### {topic.title}", *[f"- {p}" for p in topic.points], ""]
    for header, items in (("notes.decisions", notes.decisions), ("notes.open_questions", notes.open_questions)):
        out += [f"## {t(header, lang)}", "", *([f"- {x}" for x in items] or [none]), ""]
    out += [f"## {t('notes.action_items', lang)}", ""]
    if not notes.action_items:
        out.append(none)
    for a in notes.action_items:
        owner = a.owner_name or t("notes.unassigned", lang)
        due = f" — {t('notes.due', lang, due=a.due)}" if a.due else ""
        ts = f" [{fmt_ts(a.t0)}]" if a.t0 is not None else ""
        out.append(f"- [ ] **{a.title}** — {owner}{due}{ts}")
        if a.quote:
            out.append(f"  > {a.quote}")
    out += ["", f"## {t('notes.participants', lang)}", "",
            *[f"- {s.name}" for s in meeting.human_speakers], "",
            f"[[transcript|{t('notes.transcript', lang)}]]"]
    return "\n".join(out) + "\n"


def write_notes(folder: Path, meeting: Meeting, notes: Notes, lang: str) -> None:
    atomic_write_text(folder / NOTES_JSON, _dump(notes.to_dict()))
    atomic_write_text(folder / TASKS_JSON, _dump([a.to_dict() for a in notes.action_items]))
    atomic_write_text(folder / NOTES_MD, render_notes_md(meeting, notes, lang))


def read_notes(folder: Path) -> Optional[Notes]:
    p = folder / NOTES_JSON
    return Notes.from_dict(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None
