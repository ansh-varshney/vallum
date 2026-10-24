"""The Notifier (B22): live status comments, owner alerts and the Ledger mirror.

Everything here is code filling fixed templates from the state store, so it can't misreport.
Comments are posted with the owner token and start with "🛡 Vallum:". Notifications never
fail a release: errors are printed and swallowed."""

import hashlib
import os
import traceback

import httpx

from datetime import timedelta

from vallum.config import iso, utcnow

PREFIX = "🛡 Vallum:"
STATUS_MARK = "<!-- vallum:status -->"
ICONS = {"done": "✅", "running": "⏳", "failed": "❌", "rolled_back": "↩️", "skipped": "➖", "paused": "⏸️", "waiting": "🕓"}
TIER_NAMES = {"core": "Core", "standard": "Standard", "experimental": "Experimental"}


def hhmm(ts: str | None) -> str:
    return ts[11:16] if ts and len(ts) >= 16 else ""


def render_status(tl: dict) -> str:
    ref = ("!" if tl.get("kind") == "merge_requests" else "#") + str(tl.get("iid", "?"))
    head = f"{PREFIX} **Live status** · {ref} {tl.get('title', '')}".rstrip()
    bits = []
    if tl.get("tier"):
        bits.append(f"tier **{TIER_NAMES.get(tl['tier'], tl['tier'])}**")
    if tl.get("priority"):
        bits.append(f"priority **{tl['priority']}**")
    if tl.get("release_id"):
        bits.append(f"release `{tl['release_id']}`")
    bits.append(f"updated {hhmm(iso())} UTC")
    lines = [head + " · " + " · ".join(bits), STATUS_MARK, ""]
    steps = []
    for m in tl.get("milestones", []):
        label = f"{ICONS.get(m['state'], '•')} {m['step']} {hhmm(m.get('at'))}".rstrip()
        if m.get("note"):
            label += f" ({m['note']})"
        steps.append(label)
    lines.append(" · ".join(steps) if steps else "🕓 Waiting for the Merge Desk")
    lines.append("")
    lines.append(f"**Now:** {tl.get('now', 'waiting')}")
    lines.append(f"**You need to:** {tl.get('action', 'nothing. This comment updates itself.')}")
    return "\n".join(lines)


def render_alert(event: str, author: str | None, what: str, impact: str, done: str, nxt: str, action: str) -> str:
    mention = f" · @{author}" if author else ""
    return "\n".join([
        f"{PREFIX} **{event}**{mention}",
        "",
        f"**What happened:** {what}",
        f"**Impact:** {impact}",
        f"**What Vallum did:** {done}",
        f"**What happens next:** {nxt}",
        f"**Do you need to act?** {action}",
    ])


def render_ledger(release: dict, meta: dict, modes: dict, queue: list, recent: list, usage: dict | None = None,
                  cap: int | None = None, breaker_hours: float = 6) -> str:
    rows = []
    for tier in ("core", "standard", "experimental", "app"):
        m = modes.get(tier, {})
        rows.append(f"| {tier} | {round(100 * m.get('allowance', 1.0))}% | {m.get('mode', 'green').upper()} |")
    slot = release.get("slot", "idle")
    current = f"`{release.get('release_id')}` {release.get('feature', '')} ({slot})" if slot != "idle" else "idle"
    cutoff = iso(utcnow() - timedelta(hours=breaker_hours))
    breaker = len([t for t in meta.get("rollbacks", []) if str(t) >= cutoff])
    q = "\n".join(f"{i}. {item}" for i, item in enumerate(queue, 1)) or "_empty_"
    rec = "\n".join(f"- {e.get('at', '')[:16].replace('T', ' ')} · **{e.get('kind')}** · {e.get('summary')}" for e in recent[:12]) or "_nothing yet_"
    safe = f"**ON**: {meta.get('safe_mode_reason', '')}" if meta.get("safe_mode") else "off"
    day = utcnow().strftime("%Y-%m-%d")
    by_key = ", ".join(f"{k.split(':', 1)[1]} {v}" for k, v in sorted((usage or {}).items()) if k.startswith(day + ":"))
    agents_line = f"{int((usage or {}).get(day, 0))}" + (f" of {cap}" if cap else "") + (f" ({by_key})" if by_key else "")
    return f"""{PREFIX} **Reliability Ledger** (a mirror of the state store; rewritten by code, never edited by hand)

_Updated {iso()[:16].replace('T', ' ')} UTC_

| Tier | Allowance left | Mode |
|---|---|---|
{chr(10).join(rows)}

**Release:** {current} · **Safe mode:** {safe} · **Circuit breaker:** {breaker} rollback(s) in the window

**Agent sessions started by code today:** {agents_line} (the credit guard turns Safe mode on at the cap)

### Queue
{q}

### Recent
{rec}
"""


class Notifier:
    def __init__(self, gitlab, store, policy):
        self.gl = gitlab
        self.store = store
        self.policy = policy

    # status comment (FR-COMM-1) ------------------------------------------------------
    def status(self, kind: str, iid: int, step: str, state: str, note: str = "", now: str | None = None,
               action: str | None = None, **header):
        target = f"{kind}-{iid}"
        try:
            self.store.add_milestone(target, step, state, note, kind=kind, iid=iid, **header)
            extra = {}
            if now is not None:
                extra["now"] = now
            if action is not None:
                extra["action"] = action
            tl = self.store.set_timeline(target, **extra) if extra else self.store.timeline(target)
            self._write_status(kind, iid, target, tl)
        except Exception:
            print(f"[notify] status update failed for {target}:\n{traceback.format_exc()}")

    def _write_status(self, kind, iid, target, tl):
        if not self.gl.can_write:
            return
        body = render_status(tl)
        self._upsert(kind, iid, target, "note_id", tl.get("note_id"), body)
        # Door A: the requester watches the issue, so the same card lives there too
        if kind == "merge_requests" and tl.get("issue_iid"):
            self._upsert("issues", int(tl["issue_iid"]), target, "issue_note_id", tl.get("issue_note_id"), body)

    def _upsert(self, kind, iid, target, field, note_id, body):
        if note_id:
            try:
                self.gl.edit_note(kind, iid, note_id, body)
                return
            except Exception:
                pass
        note = self.gl.add_note(kind, iid, body)
        self.store.set_timeline(target, **{field: note["id"]})

    # alerts (FR-COMM-2, FR-COMM-4) -----------------------------------------------------
    def alert(self, kind: str, iid: int | None, event: str, *, author: str | None, what: str, impact: str,
              done: str, nxt: str, action: str = "No."):
        body = render_alert(event, author, what, impact, done, nxt, action)
        try:
            if self.gl.can_write and iid:
                self.gl.add_note(kind, iid, body)
            ledger = self.ledger_iid()
            if self.gl.can_write and ledger and not (kind == "issues" and iid == ledger):
                self.gl.add_note("issues", ledger, body)
        except Exception:
            print(f"[notify] alert failed:\n{traceback.format_exc()}")
        self._webhook(body)
        print("\n" + body + "\n")

    def _webhook(self, text: str):
        url = os.environ.get("ALERT_WEBHOOK_URL")
        if not url:
            return
        try:
            httpx.post(url, json={"content": text[:1900], "text": text}, timeout=10)
        except Exception:
            print("[notify] webhook failed")

    # Ledger mirror (FR-STATE-2) ---------------------------------------------------------
    def ledger_iid(self) -> int | None:
        meta = self.store.doc("meta")
        if meta.get("ledger_iid"):
            return int(meta["ledger_iid"])
        if not self.gl.can_write:
            return None
        label = self.policy.label("ledger")
        found = self.gl.get("/projects/:id/issues", labels=label, state="opened", per_page=1)
        if found:
            iid = found[0]["iid"]
        else:
            iid = self.gl.create_issue("🛡 Vallum Ledger", "Created by Vallum. Rewritten by code after every change.", [label])["iid"]
        self.store.update("meta", lambda cur: {**cur, "ledger_iid": iid})
        return iid

    def ledger(self):
        try:
            iid = self.ledger_iid()
            if not iid:
                return
            body = render_ledger(self.store.release(), self.store.doc("meta"), self.store.doc("modes"),
                                 self.store.doc("queue").get("items", []), self.store.doc("recent").get("events", []),
                                 self.store.doc("usage"),
                                 int(self.policy.get("credits", {}).get("daily_code_started_sessions", 0)) or None,
                                 float((self.policy.get("circuit_breaker") or {}).get("within_hours", 6)))
            # only rewrite when something changed: every rewrite adds a system note to the Ledger issue
            content = "\n".join(ln for ln in body.splitlines() if not ln.startswith("_Updated "))
            digest = hashlib.sha256(content.encode()).hexdigest()[:16]
            if self.store.doc("meta").get("ledger_digest") == digest:
                return
            self.store.update("meta", lambda cur: {**cur, "ledger_digest": digest})
            self.gl.edit_issue(iid, description=body)
        except Exception:
            print(f"[notify] Ledger update failed:\n{traceback.format_exc()}")


def stamp() -> str:
    return utcnow().strftime("%H:%M:%S")
