"""Post-release housekeeping (B29): after a release, code opens one merge request that refreshes the code map
and drops the watch promises that expired with the release. It carries the label `vallum-housekeeping`; the
Gatekeeper waives agent review for it only if it really is just that (no agent commits, only the code map and
cards that differ from main in nothing but their `watch` list). Watch promises are temporary by design
(`promises.watch_promises_expire`); the permanent promises of a card are never touched."""

import re
from pathlib import Path

from vallum import cards as cardlib
from vallum import codemap

_TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def drop_watch(text: str) -> str | None:
    """The card text without its top-level `watch:` block, or None if there is none or it can't be removed
    cleanly (the result must parse to the same card minus `watch`)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(r"^watch\s*:", ln)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if ln.strip() == "" or ln[0] in " \t-#":
            end += 1
            continue
        if _TOP_KEY.match(ln):
            break
        end += 1
    while end > start + 1 and lines[end - 1].strip() == "":   # keep the blank line before the next key
        end -= 1
    new = "".join(lines[:start] + lines[end:])
    try:
        old_card, new_card = cardlib.parse_card_text(text), cardlib.parse_card_text(new)
    except Exception:
        return None
    old_card.pop("watch", None)
    return new if old_card == new_card and "watch" not in new_card else None


def changes(root: Path, card_paths: list[str], main_file) -> list[dict]:
    """Commit actions for the housekeeping MR. `main_file(path)` returns the file on main (or None)."""
    cards = []
    for path in sorted(set(card_paths)):
        text = main_file(path)
        if not text:
            continue
        new = drop_watch(text.replace("\r\n", "\n"))
        if new is not None:
            cards.append({"action": "update", "file_path": path, "content": new,
                          "dropped": [w.get("id") for w in cardlib.parse_card_text(text).get("watch") or []]})
    # the map is made from the cards as they will be after this MR (seen live: a map listing the dropped watch)
    fresh_map = codemap.generate(root, {a["file_path"]: a["content"] for a in cards})
    map_path = codemap.output_path(root).relative_to(root).as_posix()
    on_main = (main_file(map_path) or "").replace("\r\n", "\n")
    actions = []
    if _without_line_numbers(on_main) != _without_line_numbers(fresh_map):
        actions.append({"action": "update" if on_main else "create", "file_path": map_path, "content": fresh_map})
    return actions + cards


def _without_line_numbers(text: str) -> str:
    """Line numbers and line counts move with every edit; only open an MR when the map really changed."""
    return re.sub(r", \d+ lines\)", ")", re.sub(r":\d+(-\d+)?\b", "", text))


def open_mr(gl, pol, release_id: str, actions: list[dict]) -> dict | None:
    if not actions:
        return None
    label = pol.label("housekeeping")
    older = [m for m in gl.open_mrs(pol.label("opt_in")) or []
             if str(m.get("source_branch", "")).startswith("vallum/housekeeping-") or label in m.get("labels", [])]
    for old in older:
        gl.put(f"/projects/:id/merge_requests/{old['iid']}", json={"state_event": "close"})
        gl.add_note("merge_requests", old["iid"], f"🛡 Vallum: replaced by the housekeeping after {release_id}.")
    branch = f"vallum/housekeeping-{release_id}"
    what = []
    if any(a["file_path"].endswith("CODEMAP.md") for a in actions):
        what.append("refresh the code map")
    cards_changed = [a["file_path"] for a in actions if a["file_path"].startswith(cardlib.CARDS_DIR)]
    if cards_changed:
        what.append(f"drop expired watch promises ({', '.join(Path(p).name for p in cards_changed)})")
    dropped = [(Path(a["file_path"]).name, w) for a in actions for w in a.get("dropped") or []]
    commit = [{k: v for k, v in a.items() if k != "dropped"} for a in actions]
    gl.post("/projects/:id/repository/commits", json={
        "branch": branch, "start_branch": pol["project"].get("default_branch", "main"),
        "commit_message": f"Housekeeping after {release_id}: " + "; ".join(what), "actions": commit})
    return gl.post("/projects/:id/merge_requests", json={
        "source_branch": branch, "target_branch": pol["project"].get("default_branch", "main"),
        "remove_source_branch": True, "labels": f"{pol.label('opt_in')},{label}",
        "title": f"Housekeeping after {release_id}: " + " and ".join(what),
        "description": ("🛡 Vallum: made by code after a release, not by an agent.\n\n"
                        + "\n".join(f"- {w}" for w in what)
                        + ("\n\n**Expired watch promises** (temporary checks raised for a change; they expire once "
                           f"{release_id} has shipped that change, per `promises.watch_promises_expire`):\n"
                           + "\n".join(f"- `{w}` in `{card}`" for card, w in dropped) if dropped else "")
                        + "\n\nPermanent promises are never changed here. The Gatekeeper waives agent review only while "
                          "this MR changes nothing but the code map and the cards' `watch` blocks; any other change, "
                          "or any agent commit, sends it through the full review.")})
