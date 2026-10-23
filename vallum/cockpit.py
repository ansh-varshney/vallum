"""Your controls (B23): exact `@vallum` commands in comments, carried out by code on main, for members only.

The Cockpit agent (a flow started by mentioning its bot) only explains and suggests commands; it never acts.
Code scans recent comments, parses the exact syntax, checks the commenter's role through the members API,
carries the command out, confirms it on the same issue or MR and records it in the audit trail."""

import re
from datetime import timedelta

import yaml

from vallum.checker.measure import parse_measure
from vallum.config import iso, utcnow

COMMANDS = ("status", "pause", "resume", "rollback", "ship-anyway", "explain", "tighten")
_CMD = re.compile(r"(?:^|\s)@vallum\s+(" + "|".join(re.escape(c) for c in COMMANDS) + r")\b[ \t]*([^\n]*)", re.I)
_REF = re.compile(r"([!#])(\d+)")
DOC = "cockpit"


def parse(body: str) -> list[tuple[str, str]]:
    """All commands in a comment: [(command, argument text)]. Text in code blocks is ignored."""
    text = re.sub(r"```.*?```", "", body or "", flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return [(m.group(1).lower(), m.group(2).strip()) for m in _CMD.finditer(text)]


def parse_ref(arg: str) -> tuple[str, int] | None:
    m = _REF.search(arg or "")
    if not m:
        return None
    return ("merge_requests" if m.group(1) == "!" else "issues", int(m.group(2)))


def tightened(measure: str, new_limit_text: str) -> str:
    """The same measure with a stricter limit; raises ValueError if it would not be stricter."""
    old = parse_measure(measure)
    unit_measure = parse_measure(f"{old.func}(latency, route='x') < {new_limit_text}") if old.func != "error_rate" \
        else parse_measure(f"error_rate(route='x') < {new_limit_text}")
    if unit_measure.limit >= old.limit:
        raise ValueError(f"{new_limit_text} is not stricter than the current limit")
    head = measure.rsplit("<", 1)[0].rstrip()
    return f"{head} < {new_limit_text.strip()}"


class Cockpit:
    def __init__(self, gl, store, pol, notify, flows=None, rollback=None):
        self.gl, self.store, self.pol, self.notify = gl, store, pol, notify
        self.flows = flows
        self.rollback = rollback          # callable(feature, who) -> str, provided by the patrol (needs the cloud login)

    def run(self, lookback_minutes: float = 60) -> list[str]:
        state = self.store.doc(DOC)
        since = state.get("last_scan") or iso(utcnow() - timedelta(minutes=lookback_minutes))
        done = set(state.get("done", []))
        started = iso()
        handled = []
        bots = set(self.flows.bots()) if self.flows else set()
        for kind in ("issues", "merge_requests"):
            for item in self.gl.recent(kind, since):
                for note in self.gl.notes(kind, item["iid"]):
                    if note.get("system") or str(note["id"]) in done or (note.get("created_at") or "") < since:
                        continue
                    author = note.get("author") or {}
                    if author.get("username") in bots or (note.get("body") or "").startswith("🛡 Vallum"):
                        continue
                    for cmd, arg in parse(note.get("body", "")):
                        handled.append(self.execute(kind, item["iid"], note, cmd, arg))
                    done.add(str(note["id"]))
        self.store.update(DOC, lambda cur: {**cur, "last_scan": started, "done": sorted(done)[-1000:]})
        return handled

    # ------------------------------------------------------------------ execution
    def reply(self, kind, iid, text):
        self.gl.add_note(kind, iid, f"🛡 Vallum: **Command** · {text}")

    def execute(self, kind: str, iid: int, note: dict, cmd: str, arg: str) -> str:
        who = (note.get("author") or {}).get("username", "?")
        access = self.gl.member_access((note.get("author") or {}).get("id", 0))
        if access < int(self.pol.get("cockpit", {}).get("min_access", 30)):
            self.store.event("command", f"refused `@vallum {cmd}` from @{who}: not a project member with Developer role")
            self.audit(who, cmd, arg, kind, iid, "refused: not a project member with Developer role or higher")
            self.reply(kind, iid, f"`@vallum {cmd}` refused: @{who} is not a project member with Developer role or higher.")
            return f"refused {cmd} from {who}"
        try:
            result = getattr(self, "do_" + cmd.replace("-", "_"))(kind, iid, arg, who)
        except Exception as exc:  # a command must never crash the patrol
            result = f"could not carry out `@vallum {cmd} {arg}`: {exc}"
        self.store.event("command", f"@{who}: @vallum {cmd} {arg}".strip() + f" → {result[:120]}")
        self.audit(who, cmd, arg, kind, iid, result)
        self.reply(kind, iid, f"`@vallum {cmd} {arg}`".replace(" `", "`") + f" from @{who}: {result}")
        return f"{cmd} by {who}"

    def audit(self, who, cmd, arg, kind, iid, result):
        record = self.store.load_release("commands") or {"release": "commands", "decisions": []}
        record["decisions"] = (record.get("decisions") or [])[-200:] + [
            {"by": f"@{who}", "command": cmd, "arg": arg, "on": f"{kind}/{iid}", "result": result, "at": iso()}]
        self.store.save_release("commands", record)

    def do_status(self, kind, iid, arg, who) -> str:
        modes = self.store.doc("modes")
        meta = self.store.doc("meta")
        rel = self.store.release()
        tiers = ", ".join(f"{t} {round(100 * (modes.get(t) or {}).get('allowance', 1))}% "
                          f"{((modes.get(t) or {}).get('mode') or 'green').upper()}" for t in ("core", "standard", "experimental", "app"))
        queue = self.store.doc("queue").get("items", [])
        return (f"done.\n\n**Modes:** {tiers}\n\n**Release:** {rel.get('slot', 'idle')} {rel.get('release_id') or ''} · "
                f"**Safe mode:** {'ON (' + str(meta.get('safe_mode_reason')) + ')' if meta.get('safe_mode') else 'off'}\n\n"
                f"**Queue:** {'; '.join(queue[:5]) or 'empty'}")

    def do_pause(self, kind, iid, arg, who) -> str:
        self.store.update("meta", lambda cur: {**cur, "safe_mode": True, "safe_mode_reason": f"paused by @{who}",
                                               "safe_mode_at": iso()})
        self.notify.alert("issues", None, "Safe mode on", author=self.pol.owners[0] if self.pol.owners else None,
                          what=f"@{who} paused Vallum with `@vallum pause`.",
                          impact="No new merges or releases. A release in progress finishes or rolls back.",
                          done="Safe mode is on.", nxt="Everything stays paused until a member says `@vallum resume`.",
                          action="Only to resume.")
        return "done: Safe mode is on. No new releases until `@vallum resume`."

    def do_resume(self, kind, iid, arg, who) -> str:
        self.store.update("meta", lambda cur: {**cur, "safe_mode": False, "safe_mode_reason": None, "rollbacks": [],
                                               "resumed_by": who, "resumed_at": iso()})
        self.notify.alert("issues", None, "Resumed", author=self.pol.owners[0] if self.pol.owners else None,
                          what=f"@{who} resumed Vallum with `@vallum resume`.", impact="Releases can start again.",
                          done="Safe mode is off and the circuit breaker is reset.",
                          nxt="The Merge Desk picks up the queue on its next run.", action="No.")
        return "done: Safe mode is off and the circuit breaker is reset."

    def do_rollback(self, kind, iid, arg, who) -> str:
        feature = arg.split()[0] if arg else ""
        if not feature:
            return "say which feature, for example `@vallum rollback <feature>`."
        if not self.rollback:
            return "rollback needs the cloud login; it runs in the Live Patrol."
        return self.rollback(feature, who)

    def do_ship_anyway(self, kind, iid, arg, who) -> str:
        ref = parse_ref(arg) or (kind, iid)
        target = f"{ref[0]}-{ref[1]}"
        self.store.set_timeline(target, override={"by": who, "at": iso()})
        try:
            if ref[0] == "merge_requests":
                self.gl.mr_labels(ref[1], add=[self.pol.label("override")], remove=[self.pol.label("paused")])
            else:
                self.gl.edit_issue(ref[1], add_labels=self.pol.label("override"), remove_labels=self.pol.label("paused"))
        except Exception:
            pass
        sign = "!" if ref[0] == "merge_requests" else "#"
        self.notify.alert(ref[0], ref[1], "Override used", author=None,
                          what=f"@{who} overrode the freeze for {sign}{ref[1]} with `@vallum ship-anyway`.",
                          impact="This change may ship while the tier it touches is Red.",
                          done="Recorded in the audit trail; every other rule still applies.",
                          nxt="The Merge Desk treats it as allowed by the mode rule.", action="No (recorded).")
        return f"done: the freeze no longer holds {sign}{ref[1]} (all other rules still apply)."

    def do_explain(self, kind, iid, arg, who) -> str:
        ref = parse_ref(arg) or (kind, iid)
        bot = self.flows.registry("cockpit").get("service_account") if self.flows else None
        if not bot:
            return "the Cockpit agent isn't enabled yet."
        sign = "!" if ref[0] == "merge_requests" else "#"
        self.gl.add_note(kind, iid, f"@{bot} explain {sign}{ref[1]} for @{who} (asked with `@vallum explain`)")
        return "asked the Cockpit agent; its answer is an AI-written summary with links to the evidence."

    def do_tighten(self, kind, iid, arg, who) -> str:
        parts = arg.split()
        if len(parts) < 2:
            return "say which promise and the new limit, for example `@vallum tighten <promise-id> 120ms`."
        pid, new = parts[0], " ".join(parts[1:])
        for entry in self.gl.tree("promises/features", ref="main"):
            if entry["type"] != "blob":
                continue
            text = self.gl.file_raw(entry["path"], "main")
            card = yaml.safe_load(text) or {}
            for ptype, plist in (card.get("promises") or {}).items():
                for p in plist or []:
                    if p.get("id") == pid and "measure" in p:
                        new_measure = tightened(p["measure"], new)
                        updated = text.replace(p["measure"], new_measure, 1)
                        branch = f"vallum/tighten-{pid}-{utcnow().strftime('%H%M%S')}"
                        self.gl.post("/projects/:id/repository/commits", json={
                            "branch": branch, "start_branch": "main", "commit_message": f"Tighten {pid}: {new_measure}",
                            "actions": [{"action": "update", "file_path": entry["path"], "content": updated}]})
                        mr = self.gl.post("/projects/:id/merge_requests", json={
                            "source_branch": branch, "target_branch": "main", "remove_source_branch": True,
                            "title": f"Tighten promise {pid} ({new})", "labels": self.pol.label("opt_in"),
                            "description": f"Requested by @{who} with `@vallum tighten`.\n\n`{p['measure']}` → `{new_measure}`"})
                        return f"opened !{mr['iid']} to make `{pid}` stricter; it goes through the usual checks."
        return f"no metric promise called `{pid}` on main."
