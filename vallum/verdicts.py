"""Reading agents' structured verdicts from merge request notes. A verdict only counts if the note was
written by the expected flow's own bot account (checked by code against the flows registry), and its
JSON parses. Anyone can type the marker; nobody but the bot can author as the bot."""

import json
import re

MARKERS = {"review": "<!-- vallum:review -->", "inspector": "<!-- vallum:inspector -->"}
EXPECTED_BOT = {"review": "ship", "inspector": "inspect"}
_JSON = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def parse(body: str, kind: str) -> dict | None:
    if MARKERS[kind] not in (body or ""):
        return None
    m = _JSON.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def latest(notes: list[dict], bots: dict) -> dict:
    """Newest valid verdict of each kind, from the right author only. bots: username -> flow key."""
    found = {}
    for note in notes:  # oldest first, so later notes win
        author = (note.get("author") or {}).get("username")
        for kind in MARKERS:
            data = parse(note.get("body", ""), kind)
            if data is None:
                continue
            if bots.get(author) != EXPECTED_BOT[kind]:
                continue  # wrong author: ignored, never trusted
            found[kind] = {**data, "note_id": note.get("id"), "at": note.get("created_at"), "author": author}
    return found
