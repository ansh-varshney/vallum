"""Your controls (B23): exact `@vallum` commands in comments, carried out by code on main, for members only.

The Cockpit agent (a flow started by mentioning its bot) only explains and suggests commands; it never acts.
Code scans recent comments, parses the exact syntax, checks the commenter's role through the members API,
carries the command out, confirms it on the same issue or MR and records it in the audit trail.

Commands may start with `@vallum` or with the Cockpit bot's name (GitLab's autocomplete offers the bot when
you type @vallum); mentioning the bot also makes the Cockpit agent acknowledge at once. After a command, code
listens for follow-ups every few seconds for a while (listen mode), and `@vallum listen <minutes>` keeps it
listening for a whole test or demo session."""

import re
import time
from datetime import timedelta

import yaml

from vallum.checker.measure import parse_measure
from vallum.config import iso, parse_iso, utcnow

COMMANDS = ("status", "pause", "resume", "rollback", "ship-anyway", "explain", "tighten", "listen")
_REF = re.compile(r"([!#])(\d+)")
DOC = "cockpit"


def _pattern(names: tuple[str, ...]) -> re.Pattern:
    who = "|".join(re.escape(n) for n in names)
    return re.compile(r"(?:^|\s)@(?:" + who + r")\s+(" + "|".join(re.escape(c) for c in COMMANDS)
                      + r")\b[ \t]*([^\n]*)", re.I)


def parse(body: str, names: tuple[str, ...] = ("vallum",)) -> list[tuple[str, str]]:
    """All commands in a comment: [(command, argument text)]. Text in code blocks is ignored. `names` are the
    accepted prefixes: `vallum` and the Cockpit bot's username."""
    text = re.sub(r"```.*?```", "", body or "", flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return [(m.group(1).lower(), m.group(2).strip()) for m in _pattern(names).finditer(text)]


def tightened_says(says: str, measure: str, new_limit_text: str) -> str:
    """The promise's plain-English text with the old limit replaced by the new one ("under 300 ms" -> "under
    250 ms"), so the words and the check never disagree. If the old limit isn't in the text, it is added."""
    old = measure.rsplit("<", 1)[1].strip()
    m = re.match(r"([\d.]+)\s*([a-z%]*)", old, re.I)
    n = re.match(r"([\d.]+)\s*([a-z%]*)", new_limit_text.strip(), re.I)
    if not m or not n:
        return says
    pattern = re.compile(r"(?<![\d.])" + re.escape(m.group(1)) + r"(\s*)" + re.escape(m.group(2)) + r"(?![a-z])", re.I)
    if pattern.search(says):
        return pattern.sub(lambda x: n.group(1) + x.group(1) + (n.group(2) or m.group(2)), says, count=1)
    return f"{says} (limit {new_limit_text.strip()})"


def is_ours(body: str) -> bool:
    """Notes Vallum's code wrote itself (with the owner's account): never read back as commands."""
    return body.lstrip().startswith("🛡 Vallum") or "(asked with `@vallum explain`)" in body


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

    def names(self) -> tuple[str, ...]:
        bot = self.flows.registry("cockpit").get("service_account") if self.flows else None
        return ("vallum", bot) if bot else ("vallum",)

    def claim(self, key: str) -> bool:
        """Mark one command as handled in a transaction; False if another scanner already took it."""
        got = {"ok": False}

        def fn(cur):
            done = list(cur.get("done", []))
            got["ok"] = key not in done
            if got["ok"]:
                cur["done"] = (done + [key])[-2000:]
            return cur

        self.store.update(DOC, fn)
        return got["ok"]

    def run(self, lookback_minutes: float = 60) -> list[str]:
        state = self.store.doc(DOC)
        # Re-read a generous window every time: GitLab.com may answer from a replica that is a few seconds behind,
        # so a comment made just before the last scan can still be missing from it. Claims stop double runs.
        rescan = timedelta(minutes=float(self.pol.get("cockpit", {}).get("rescan_minutes", 30)))
        last = parse_iso(state.get("last_scan"))
        since = iso(min(last, utcnow() - rescan) if last else utcnow() - timedelta(minutes=lookback_minutes))
        done = set(state.get("done", []))
        started = iso()
        handled = []
        bots = set(self.flows.bots()) if self.flows else set()
        names = self.names()
        for kind in ("issues", "merge_requests"):
            for item in self.gl.recent(kind, since):
                for note in self.gl.notes(kind, item["iid"], limit=100):
                    changed = note.get("updated_at") or note.get("created_at") or ""
                    if note.get("system") or parse_iso(changed) < parse_iso(since):
                        continue
                    author = note.get("author") or {}
                    if author.get("username") in bots or is_ours(note.get("body") or ""):
                        continue
                    # each command in a comment runs once; a comment edited to add a command runs only the new one
                    for cmd, arg in parse(note.get("body", ""), names):
                        key = f"{note['id']}:{cmd} {arg}".strip()
                        if key in done or str(note["id"]) in done or not self.claim(key):
                            continue
                        handled.append(self.execute(kind, item["iid"], note, cmd, arg))
        self.store.update(DOC, lambda cur: {**cur, "last_scan": started})
        return handled

    def listening_until(self):
        return parse_iso(self.store.doc(DOC).get("listen_until"))

    def listen(self, budget_s: float, poll_s: float = 15, sleep=time.sleep, clock=time.monotonic,
               tick=None) -> list[str]:
        """Scan once; then, while listen mode is on, scan again every `poll_s` seconds for up to `budget_s`.
        `tick` (the MR watcher) runs on every pass; any change on a Vallum MR switches listen mode on."""
        handled = self.run()
        self._tick(tick)
        end = clock() + budget_s
        while clock() + poll_s < end:
            until = self.listening_until()
            if not until or until <= utcnow():
                break
            sleep(poll_s)
            handled += self.run()
            self._tick(tick)
        return handled

    def _tick(self, tick):
        if not tick:
            return
        try:
            if tick():
                self.keep_listening(float(self.pol.get("cockpit", {}).get("listen_after_command_min", 20)))
        except Exception as exc:  # watching must never stop the listener
            print(f"[cockpit] watch failed: {type(exc).__name__}: {exc}", flush=True)

    def keep_listening(self, minutes: float):
        until = iso(utcnow() + timedelta(minutes=minutes))
        self.store.update(DOC, lambda cur: {**cur, "listen_until": max(until, cur.get("listen_until") or "")})

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
        if not (cmd == "listen" and arg.strip().lower() == "off"):
            # people give commands in bursts: answer the next ones within seconds
            self.keep_listening(float(self.pol.get("cockpit", {}).get("listen_after_command_min", 20)))
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
        until = self.listening_until()
        listening = f"on until {until.strftime('%H:%M')} UTC" if until and until > utcnow() else "off"
        return (f"done.\n\n**Modes:** {tiers}\n\n**Release:** {rel.get('slot', 'idle')} {rel.get('release_id') or ''} · "
                f"**Safe mode:** {'ON (' + str(meta.get('safe_mode_reason')) + ')' if meta.get('safe_mode') else 'off'} · "
                f"**Listen mode:** {listening}\n\n"
                f"**Queue:** {'; '.join(queue[:5]) or 'empty'}")

    def do_listen(self, kind, iid, arg, who) -> str:
        word = (arg.split() or [""])[0].lower()
        if word == "off":
            self.store.update(DOC, lambda cur: {**cur, "listen_until": None})
            return "done: listen mode is off. Commands are carried out on the next Live Patrol (within about 10 minutes)."
        cap = float(self.pol.get("cockpit", {}).get("listen_max_min", 180))
        try:
            minutes = min(cap, max(1.0, float(word))) if word else 60.0
        except ValueError:
            return "say for how long, for example `@vallum listen 120` (minutes), or `@vallum listen off`."
        self.store.update(DOC, lambda cur: {**cur, "listen_until": iso(utcnow() + timedelta(minutes=minutes))})
        return (f"done: listen mode is on for {minutes:g} minutes. Commands are carried out within about "
                f"{self.pol.get('cockpit', {}).get('poll_s', 15)} seconds while a Live Patrol is listening.")

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
        self.gl.add_note(kind, iid, f"🛡 Vallum: @{bot} please explain {sign}{ref[1]} for @{who} (asked with `@vallum explain`)")
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
                        says = p.get("says") or ""
                        new_says = tightened_says(says, p["measure"], new)
                        if says and new_says != says:
                            updated = updated.replace(says, new_says, 1)
                        branch = f"vallum/tighten-{pid}-{utcnow().strftime('%H%M%S')}"
                        self.gl.post("/projects/:id/repository/commits", json={
                            "branch": branch, "start_branch": "main", "commit_message": f"Tighten {pid}: {new_measure}",
                            "actions": [{"action": "update", "file_path": entry["path"], "content": updated}]})
                        mr = self.gl.post("/projects/:id/merge_requests", json={
                            "source_branch": branch, "target_branch": "main", "remove_source_branch": True,
                            "title": f"Tighten promise {pid} ({new})", "labels": self.pol.label("opt_in"),
                            "description": f"Requested by @{who} with `@vallum tighten`.\n\n`{p['measure']}` → `{new_measure}`"
                                           + (f"\n\n\"{says}\" → \"{new_says}\"" if new_says != says else "")})
                        return f"opened !{mr['iid']} to make `{pid}` stricter; it goes through the usual checks."
        return f"no metric promise called `{pid}` on main."
