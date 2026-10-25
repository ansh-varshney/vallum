"""Live Patrol (scheduled main pipeline): the Watchdog, live promise checks, mistake allowances and modes,
resuming paused work, starting incident fixes (Door C), catching stalled requests, carrying out `@vallum`
commands, and the Ledger. Plain code; agents are only started by member-only actions (namespace credits).

    python -m vallum.patrol
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import timedelta

import httpx

from simulate.visitors import Visitors, plan_rates
from vallum import agents, allowance, config, evidence, gcp, public
from vallum import cards as cardlib
from vallum.checker import core
from vallum.cockpit import Cockpit
from vallum.config import iso, parse_iso, utcnow
from vallum.drive.cloudrun import CloudRun
from vallum.flows import Flows
from vallum.gitlab import GitLab
from vallum.notify import Notifier
from vallum.patrol.watchdog import Watchdog
from vallum.watch import Watcher
from vallum.state import Store

ISSUES = "issues"


class StopListening(Exception):
    pass
CONFIRM_RUNS = 2          # a live break must be seen on two patrols in a row before it becomes an incident


def log(msg: str):
    print(f"[patrol {utcnow().strftime('%H:%M:%S')}] {msg}", flush=True)


def tier_of_promise(p: cardlib.Promise, cards: dict) -> str:
    return (cards.get(p.feature) or {}).get("tier", "app") if p.feature in cards else "app"


def rolled_back_on_purpose(release: dict, serving: str | None) -> bool:
    """A member rolled production back (command or rollback-now): newer features are not live, so their live
    promises can't be judged until the next release."""
    promoted = release.get("promoted_revision")
    return bool(serving and promoted and serving != promoted)


def pause_tier(notes: list[dict], bot: str | None) -> str | None:
    """The tier named in the Flight Director's latest `pause` decision note."""
    tier = None
    for n in notes:
        if bot and (n.get("author") or {}).get("username") != bot:
            continue
        body = n.get("body") or ""
        if re.search(r'"action"\s*:\s*"pause"', body):
            m = re.search(r'"tier"\s*:\s*"(\w+)"', body)
            tier = m.group(1) if m else tier
    return tier


class Patrol:
    def __init__(self, pol, gl, store):
        self.pol, self.gl, self.store = pol, gl, store
        self.notify = Notifier(gl, store, pol)
        self.flows = Flows(gl, store)
        self.cards = cardlib.load_from_dir(config.ROOT)
        self.url = config.service_url(pol)
        self._cr = None

    def cloudrun(self) -> CloudRun:
        if self._cr is None:
            cr = self.pol["cloud_run"]
            self._cr = CloudRun(gcp.session(), cr["project"], cr["region"], cr["service"])
        return self._cr

    def owner(self):
        return self.pol.owners[0] if self.pol.owners else None

    def api_key(self) -> str | None:
        return (self.store.b.read("vallum_secrets", "app") or {}).get("api_key")

    # ------------------------------------------------------------------ live checks + allowance
    def live(self):
        rel = self.store.release()
        if rel.get("slot") == "driving":
            log("a test drive is running; it measures and spends the allowance itself")
            return
        try:
            health = httpx.get(self.url + "/health", timeout=20)
            revision = health.headers.get("x-revision")
            log(f"live check: /health -> {health.status_code} ({revision})")
        except httpx.HTTPError as exc:
            log(f"live check failed: {exc}")
            revision = None
        key = self.api_key()
        cost = float(self.pol.get("allowance", {}).get("behavior_break_cost", 0.05))
        spend, broken = {}, []
        rolled_back = rolled_back_on_purpose(rel, revision)
        if rolled_back:
            log(f"serving {revision}, older than the last release {rel.get('promoted_revision')} (rolled back on "
                "purpose): live promise checks wait for the next release")
        live_cards = self.live_cards()
        checked = {}
        with core.client_for(self.url, timeout=15) as client:
            for p in ([] if rolled_back else live_cards.promises(where="live")):
                if p.style != "steps":
                    continue
                out = core.run_step_promise(client, p, key, float(self.pol["judge"]["step_retry_s"]))
                checked[f"{p.feature}/{p.id}"] = "broken" if out["verdict"] == "broken" else "kept"
                if out["verdict"] == "broken":
                    tier = tier_of_promise(p, self.cards.cards)
                    spend[tier] = spend.get(tier, 0) + cost
                    broken.append({"promise": p, "detail": out.get("detail", ""), "tier": tier})
        self.publish(live_cards, checked, revision, rolled_back)
        samples = self.visitors(key)
        tier_of, limits = allowance.route_profile(self.cards.cards)
        counts = allowance.count(samples, tier_of, limits, float(self.pol.get("allowance", {}).get("slow_factor", 2.0)),
                                 warmup_s=3)
        allowance.record(self.store, counts, "patrol", spend, self.pol.window_minutes())
        log(f"visitors: {len(samples)} visits; bad: " + ", ".join(f"{t} {c['bad']}/{c['visits']}" for t, c in counts.items()))
        self.report_broken(broken, revision)

    def publish(self, live_cards: cardlib.CardSet, checked: dict, revision: str | None, rolled_back: bool):
        """The status page's summary (B27): the live code's promises and what this run found."""
        project = os.environ.get("CI_PROJECT_URL", "")
        ledger = self.store.doc("meta").get("ledger_iid")
        try:
            public.record_live(self.store, live_cards, checked, revision,
                               self.store.live_commit(self.pol["cloud_run"]["service"]),
                               project_url=project, ledger_url=f"{project}/-/issues/{ledger}" if project and ledger else "",
                               window_minutes=self.pol.window_minutes(), rolled_back=rolled_back,
                               breaker={"limit": int(self.pol["circuit_breaker"]["rollbacks"]),
                                        "hours": float(self.pol["circuit_breaker"]["within_hours"])})
        except Exception as exc:  # the status page must never break the patrol
            log(f"could not publish the status summary: {type(exc).__name__}: {exc}")

    def live_cards(self) -> cardlib.CardSet:
        """The promises of the code that is live, not of main: a feature can be merged but not released (refused
        at step 0, or rolled back), and its promises would look broken in production (seen with the link preview)."""
        live = self.store.live_commit(self.pol["cloud_run"]["service"])
        if not live:
            return self.cards
        try:
            return cardlib.load_from_gitlab(self.gl, live)
        except Exception as exc:
            log(f"could not read the cards of the live code ({live[:8]}): {exc}; using main's")
            return self.cards

    def visitors(self, key: str | None) -> list[dict]:
        codes = (self.store.b.read("vallum_sim", "links") or {}).get("codes") or []
        if not codes or not key:
            log("no persistent links yet (the first release creates them): no visitors this run")
            return []
        seconds = float(self.pol.get("allowance", {}).get("patrol_visit_seconds", 30))
        sim = self.pol["simulate"]
        rates = plan_rates({}, 1.0, seconds, float(sim["base_rps"]), float(sim["max_rps"]), sim["mix"])
        v = Visitors(self.url, codes, key)
        asyncio.run(v.run(rates, seconds))
        return v.samples

    def report_broken(self, broken: list[dict], revision: str | None):
        state = self.store.doc("patrol")
        seen = dict(state.get("broken", {}))         # key -> {runs, first_at, incident}
        now = {}
        for b in broken:
            p = b["promise"]
            key = f"{p.feature}/{p.id}@{revision}"
            entry = {**seen.get(key, {"runs": 0, "first_at": iso()})}
            entry["runs"] = int(entry["runs"]) + 1
            now[key] = entry
            self.store.event("live", f"live promise broken ({entry['runs']}x): {p.feature}/{p.id} {b['detail'][:80]}")
            if entry["runs"] >= CONFIRM_RUNS and not entry.get("incident"):
                entry["incident"] = self.live_incident(b, revision, entry)
        self.store.update("patrol", lambda cur: {**cur, "broken": now, "checked_at": iso()})
        if broken:
            log("live broken: " + ", ".join(now))

    def live_incident(self, b: dict, revision: str | None, entry: dict) -> int | None:
        p, tier = b["promise"], b["tier"]
        record = {"source": "live patrol", "promise": f"{p.feature}/{p.id}", "says": p.says, "type": p.type,
                  "detail": b["detail"], "revision": revision, "tier": tier, "seen_runs": entry["runs"],
                  "first_seen": entry["first_at"], "allowance_spent_per_run": self.pol.get("allowance", {}).get("behavior_break_cost")}
        body = (f"🛡 Vallum: a live promise is broken in production (seen on {entry['runs']} patrols in a row).\n\n"
                f"**Promise:** `{p.feature}/{p.id}` · {p.says}\n\n**Revision:** `{revision}` · **Tier:** {tier}\n\n"
                "Nothing was rolled back automatically: the release already passed its test drive. Each patrol "
                "spends the tier's allowance while it stays broken; a Red tier pauses changes that touch it.\n\n"
                "**Incident record**\n```json\n" + json.dumps(record, indent=1) + "\n```")
        try:
            issue = self.gl.create_issue(f"Incident: live promise {p.id} broken", body, [self.pol.label("incident")])
        except Exception as exc:
            log(f"could not open the incident issue: {exc}")
            return None
        via = agents.start(self.flows, self.store, self.pol, self.notify, "incident", ISSUES, issue["iid"],
                           {"incident_iid": issue["iid"], **record}, issue_id=issue["iid"])
        self.notify.alert(ISSUES, issue["iid"], "Live promise broken", author=self.owner(),
                          what=f"`{p.feature}/{p.id}` ({p.says}) is broken in production: {b['detail']}",
                          impact=f"Spends the {tier} allowance on every patrol while it stays broken.",
                          done=f"Opened incident #{issue['iid']}" + (f" and assigned the Incident Commander ({via})." if via else "."),
                          nxt="The Incident Commander writes the postmortem, a learned promise and a fix issue; "
                              "Vallum then builds the fix by itself (Door C).",
                          action="No, unless you want to roll back now (`@vallum rollback <feature>`).")
        return issue["iid"]

    def modes(self) -> list[dict]:
        changes = allowance.update_modes(self.store, self.pol)
        allowance.announce(changes, self.store, self.notify, self.owner())
        return changes

    # ------------------------------------------------------------------ paused requests, fixes, stalls
    def restart_door_a(self, issue: dict) -> str | None:
        """Re-fire Door A the way a member would (status To do, then In progress): the Ship flow runs on its
        own service account. Falls back to the Flows API."""
        if not agents.usage_ok(self.store, self.pol, self.notify):
            return None
        errors = []
        try:
            errors = self.gl.set_status(issue, "To do") + self.gl.set_status(issue, "In progress")
        except Exception as exc:
            errors = [str(exc)]
        if not errors:
            agents.count_usage(self.store, "ship")
            return "trigger"
        log(f"#{issue['iid']}: status change failed ({errors}); using the Flows API")
        try:
            self.flows.start("ship", {"door": "A", "issue_iid": issue["iid"], "resumed": True}, issue_id=issue["iid"])
        except Exception as exc:
            log(f"#{issue['iid']}: could not start the Ship flow: {exc}")
            return None
        agents.count_usage(self.store, "ship")
        return "api"

    def open_issues(self, labels: str) -> list[dict]:
        return self.gl.paged("/projects/:id/issues", labels=labels, state="opened", limit=100)

    def resume_paused(self, modes: dict) -> list[str]:
        out = []
        if self.store.doc("meta").get("safe_mode") or (modes.get("app") or {}).get("mode") == "red":
            return out
        bot = self.flows.registry("ship").get("service_account")
        for issue in self.open_issues(self.pol.label("paused")):
            tier = pause_tier(self.gl.notes(ISSUES, issue["iid"]), bot) or "core"
            if (modes.get(tier) or {}).get("mode") == "red":
                continue
            self.gl.edit_issue(issue["iid"], remove_labels=self.pol.label("paused"))
            via = self.restart_door_a(issue)
            if not via:
                continue
            self.store.event("resumed", f"#{issue['iid']} resumed: {tier} recovered")
            self.notify.alert(ISSUES, issue["iid"], "Resumed", author=(issue.get("author") or {}).get("username"),
                              what=f"{issue['title']} was paused because {tier} was RED. It has recovered.",
                              impact="None yet.", done=f"Removed the pause and restarted the request ({via}).",
                              nxt="Vallum plans and builds it as usual; this issue shows the progress.", action="No.")
            out.append(f"resumed #{issue['iid']}")
        return out

    def start_fixes(self) -> list[str]:
        """Door C: fix issues filed by the Incident Commander are built without anyone moving them."""
        out = []
        if self.store.doc("meta").get("safe_mode"):
            return out
        for issue in self.open_issues(f"{self.pol.label('opt_in')},{self.pol.label('fix')}"):
            target = f"{ISSUES}-{issue['iid']}"
            if self.store.timeline(target).get("fix_started") or self.pol.label("needs_human") in issue.get("labels", []):
                continue
            via = self.restart_door_a(issue)
            if not via:
                continue
            self.store.set_timeline(target, fix_started=iso(), kind=ISSUES, iid=issue["iid"], title=issue["title"])
            self.store.event("door-c", f"fix #{issue['iid']} started ({via})")
            self.notify.status(ISSUES, issue["iid"], "Fix started (Door C)", "running", title=issue["title"],
                               now="Vallum found this fix issue and started building it.", action="nothing.")
            out.append(f"fix #{issue['iid']}")
        return out

    def stalled(self) -> list[str]:
        """Door A requests whose Ship session started but no merge request appeared before the deadline."""
        out = []
        deadline = timedelta(minutes=float(self.pol["watchdog"]["deadlines_min"].get("door_a_no_mr", 45)))
        bot = self.flows.registry("ship").get("service_account")
        if not bot:
            return out
        mrs = self.gl.paged("/projects/:id/merge_requests", state="all", labels=self.pol.label("opt_in"), limit=200)
        linked = {evidence.related_issue(mr.get("description")) for mr in mrs}
        skip = {self.pol.label(k) for k in ("ledger", "incident", "paused", "needs_human")}
        for issue in self.open_issues(self.pol.label("opt_in")):
            if skip & set(issue.get("labels", [])) or issue["iid"] in linked:
                continue
            starts = [n for n in self.gl.notes(ISSUES, issue["iid"])
                      if (n.get("author") or {}).get("username") == bot and "started session" in (n.get("body") or "")]
            if not starts:
                continue
            last = starts[-1]
            target = f"{ISSUES}-{issue['iid']}"
            if self.store.timeline(target).get("stalled_note") == last["id"]:
                continue
            begun = parse_iso(last.get("created_at"))
            if begun and utcnow() - begun > deadline:
                self.store.set_timeline(target, stalled_note=last["id"], stalled_at=iso())
                self.gl.edit_issue(issue["iid"], add_labels=self.pol.label("needs_human"))
                self.notify.alert(ISSUES, issue["iid"], "Work stalled", author=(issue.get("author") or {}).get("username"),
                                  what="Vallum started on this request but no merge request appeared before the deadline.",
                                  impact="None: nothing is live.", done="Labelled it vallum::needs-human.",
                                  nxt="Look at the agent session linked above. To retry, remove the label and move the "
                                      "issue to To do and back to In progress.",
                                  action="Yes, if you still want it.")
                self.store.event("needs-human", f"#{issue['iid']}: no merge request {deadline.seconds // 60} min after the start")
                out.append(f"stalled #{issue['iid']}")
        return out

    # ------------------------------------------------------------------ rollback for @vallum rollback <feature>
    def rollback_feature(self, feature: str, who: str) -> str:
        rel = self.store.release()
        # only a test drive moves traffic; a merge being checked or just merged does not (seen live: the command
        # was refused while the Merge Desk held the slot for half a minute)
        if rel.get("slot") == "driving":
            return "a test drive is moving traffic right now; use the `rollback-now` job for an instant rollback."
        records = [r for _k, r in self.store.b.list("vallum_releases")
                   if r.get("outcome") == "released" and feature in str(r.get("feature", ""))]
        records.sort(key=lambda r: r.get("finished_at", ""))
        if not records:
            return f"no release of `{feature}` is recorded."
        target = records[-1].get("previous_revision")
        if not target:
            return f"the release of `{feature}` has no earlier revision recorded (it was the first deploy)."
        cr = self.cloudrun()
        current = CloudRun.main_revision(cr.get())
        if current == target:
            return f"traffic is already on `{target}`."
        cr.set_traffic([{"revisionName": target, "percent": 100}])
        history = [r for r in rel.get("previous_good", []) if r != target]
        self.store.set_release(last_good=target, previous_good=history[:5])
        tripped = self.store.record_rollback(float(self.pol["circuit_breaker"]["within_hours"]),
                                             int(self.pol["circuit_breaker"]["rollbacks"]))
        self.store.event("rolled_back", f"@{who} rolled back {feature}: {current} → {target}")
        self.notify.alert(ISSUES, None, "Rolled back by command", author=self.owner(),
                          what=f"@{who} asked `@vallum rollback {feature}`.",
                          impact=f"`{current}` no longer serves visitors; everything released after `{target}` is undone too.",
                          done=f"Sent 100% of traffic to `{target}`. Recorded as an override.",
                          nxt=("Safe mode is now ON (circuit breaker). " if tripped else "")
                              + "The code stays in main; the next release brings it back unless it is reverted.",
                          action="Yes: say `@vallum resume` when ready." if tripped else "No.")
        return f"done: traffic is on `{target}` (was `{current}`)."

    # ------------------------------------------------------------------ run
    def run(self):
        actions = Watchdog(self.gl, self.store, self.pol, self.notify, self.cloudrun).run()
        log("watchdog: " + (", ".join(actions) if actions else "nothing to undo"))
        try:
            self.live()
        except Exception as exc:
            log(f"live checks failed: {type(exc).__name__}: {exc}")
        changes = self.modes()
        modes = self.store.doc("modes")
        log("modes: " + ", ".join(f"{t} {round(100 * (modes.get(t) or {}).get('allowance', 1))}% "
                                  f"{((modes.get(t) or {}).get('mode') or 'green').upper()}"
                                  for t in ("core", "standard", "experimental", "app"))
            + (f" · changed: {changes}" if changes else ""))
        steps = (("resume", lambda: self.resume_paused(modes)), ("fixes", self.start_fixes), ("stalled", self.stalled))
        for name, step in steps:
            try:
                done = step()
                log(f"{name}: " + (", ".join(done) if done else "nothing"))
            except Exception as exc:
                log(f"{name} failed: {type(exc).__name__}: {exc}")
        self.notify.ledger()
        cfg = self.pol.get("cockpit", {})
        cockpit = Cockpit(self.gl, self.store, self.pol, self.notify, self.flows, rollback=self.rollback_feature)
        try:
            watcher = Watcher(self.gl, self.store, self.pol, self.notify, log=log)
            checked = {"at": 0.0, "stale": False}

            def tick():
                # stop listening once main has newer code: the next patrol listens with it
                if time.monotonic() - checked["at"] > 120:
                    checked["at"] = time.monotonic()
                    checked["stale"] = bool(config.running_stale_code(self.gl))
                if checked["stale"]:
                    raise StopListening()
                return watcher.tick()

            try:
                done = cockpit.listen(budget_s=60 * float(cfg.get("job_listen_max_min", 8)),
                                      poll_s=float(cfg.get("poll_s", 15)), tick=tick)
            except StopListening:
                done = []
                log("cockpit: main has newer code; stopped listening (the next patrol takes over)")
            until = cockpit.listening_until()
            log("cockpit: " + (", ".join(done) if done else "nothing")
                + (f" (listen mode until {until.strftime('%H:%M')} UTC)" if until and until > utcnow() else ""))
            if done:
                self.notify.ledger()
        except Exception as exc:
            log(f"cockpit failed: {type(exc).__name__}: {exc}")


def main():
    pol = config.policy()
    gl = GitLab.from_env(require_token=True)
    store = Store.from_env()
    Patrol(pol, gl, store).run()
    sys.exit(0)


if __name__ == "__main__":
    main()
