"""The test-drive controller (B8). Plain code in a main-pipeline job: no agent, no credits.

    python -m vallum.drive release        deploy at 0%, step 0, the tier's traffic steps, promote or roll back
    python -m vallum.drive rollback-now   instant rollback to the last good revision (manual job, no variables)

Every step writes a verdict report, updates the state store and tells the change owner (FR-DRIVE-4).
"""

import asyncio
import json
import os
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

from simulate.visitors import Visitors, ensure_links, plan_rates
from vallum import cards as cardlib
from vallum import config, evidence, gcp
from vallum.checker import core
from vallum.checker.measure import parse_measure
from vallum.config import iso, utcnow
from vallum.drive import judge
from vallum.drive.cloudrun import CloudRun, DeployFailed
from vallum.flows import Flows
from vallum.gitlab import GitLab
from vallum.notify import Notifier
from vallum.state import Abort, Store

MR = "merge_requests"
REPORTS = Path("reports")


def log(msg: str):
    print(f"[drive {utcnow().strftime('%H:%M:%S')}] {msg}", flush=True)


class Drive:
    def __init__(self):
        self.pol = config.policy()
        self.gl = GitLab.from_env(require_token=True)
        self.store = Store.from_env()
        self.notify = Notifier(self.gl, self.store, self.pol)
        cr = self.pol["cloud_run"]
        self.cr = CloudRun(gcp.session(), cr["project"], cr["region"], cr["service"])
        self.url = config.service_url(self.pol)
        self.cards = cardlib.load_from_dir(config.ROOT)
        self.report = {"steps": []}
        REPORTS.mkdir(exist_ok=True)

    # ---------------------------------------------------------------- helpers
    def api_key(self) -> str:
        doc = self.store.b.read("vallum_secrets", "app") or {}
        if not doc.get("api_key"):
            doc = {"api_key": secrets.token_urlsafe(32), "created_at": iso()}
            self.store.b.put("vallum_secrets", "app", doc)
        return doc["api_key"]

    def sim_codes(self, key: str) -> list[str]:
        doc = self.store.b.read("vallum_sim", "links") or {}
        want = int(self.pol["simulate"]["links"])
        codes = list(doc.get("codes", []))
        if len(codes) < want:
            codes = ensure_links(self.url, key, codes, want)
            self.store.b.put("vallum_sim", "links", {"codes": codes, "updated_at": iso()})
        return codes

    def aborted(self) -> bool:
        return bool(self.store.release().get("abort"))

    def promises(self, names: list[str]) -> list[cardlib.Promise]:
        chosen = set(names) & set(self.cards.cards) or set(self.cards.cards)
        return self.cards.promises(where="drive", features=chosen)

    def needs(self, metric: list[cardlib.Promise]) -> dict:
        out = {}
        for p in metric:
            m = parse_measure(p.data["measure"])
            need = int(p.data.get("judge", {}).get("min_samples", self.pol.min_samples(m.kind)))
            out[m.route] = max(out.get(m.route, 0), need)
        return out

    def status(self, mr_iid, step, state, **kw):
        if mr_iid:
            self.notify.status(MR, int(mr_iid), step, state, **kw)

    def save_report(self, release_id: str):
        (REPORTS / f"drive-{release_id}.json").write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def audit(self, release_id: str, decision: dict, **fields):
        record = self.store.load_release(release_id) or {"release": release_id, "decisions": [], "human_actions": 0}
        record.setdefault("decisions", []).append({**decision, "at": iso()})
        record.update(fields)
        self.store.save_release(release_id, record)
        (REPORTS / f"audit-{release_id}.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    def issue_of(self, mr_iid) -> int | None:
        if not mr_iid:
            return None
        try:
            return evidence.related_issue(self.gl.mr(int(mr_iid)).get("description"))
        except Exception:
            return None

    def start_agent(self, key: str, goal: dict, issue_id=None):
        """Agents think after code acts: a failure here never changes the release outcome."""
        try:
            started = Flows(self.gl, self.store).start(key, goal, issue_id=issue_id)
            self.store.event("agent", f"{goal.get('release_id')}: started {key} session {started.get('id')}")
            log(f"started {key} flow: session {started.get('id')}")
        except Exception as exc:
            log(f"could not start the {key} flow: {exc}")

    def gitlab_release(self, info: dict, new_rev: str, kept: list[str]):
        rid = info["release_id"]
        lines = [f"**{info.get('title')}** · tier {info.get('tier')} · revision `{new_rev}`", "",
                 f"Promises kept in the test drive: {len(kept)}", ""]
        lines += [f"- `{k}`" for k in kept[:30]]
        lines += ["", f"Steps: {' → '.join(str(s['step']) + '%' for s in self.report['steps'])} → 100%",
                  "", "_Written by code from the release record._"]
        try:
            self.gl.post("/projects/:id/releases", json={
                "tag_name": rid, "ref": info["commit"], "name": f"{rid}: {info.get('title')}",
                "description": "\n".join(lines)})
        except Exception as exc:
            log(f"could not create the GitLab release: {exc}")

    # ---------------------------------------------------------------- the release
    def identify(self) -> dict:
        """Which release is this? A Merge Desk merge (slot 'merged'), or an owner push."""
        sha = os.environ["CI_COMMIT_SHA"]
        rel = self.store.release()
        prov = os.environ.get("VALLUM_PROVENANCE", "")
        if prov == "merge-desk" and rel.get("slot") == "merged" and rel.get("release_id") == os.environ.get("VALLUM_RELEASE_ID"):
            return {**rel, "commit": sha}
        before = os.environ.get("CI_COMMIT_BEFORE_SHA", "")
        files = []
        if before and set(before) != {"0"}:
            try:
                files = [d["new_path"] for d in self.gl.compare(before, sha).get("diffs", [])]
            except Exception:
                files = []
        touched = sorted(cardlib.blast_radius(files, self.cards.cards))
        changed_cards = sorted(n for n, p in self.cards.paths.items() if p in files)
        names = changed_cards or touched
        subjects = [self.cards.cards[n] for n in names]
        tier = cardlib.effective_tier(subjects, config.catalog(), set(touched), self.cards.cards, files,
                                      self.pol.get("core_paths", [])) if names else "standard"
        return {"release_id": self.store.next_release_id(), "mr_iid": None, "features": names, "touched": touched,
                "feature": ", ".join(names) or "app", "tier": tier, "author": os.environ.get("GITLAB_USER_LOGIN"),
                "title": os.environ.get("CI_COMMIT_TITLE", "owner push"), "commit": sha, "owner_push": True}

    def no_app_change(self) -> int:
        """Merged, but nothing the app runs on changed (no image was built): free the slot."""
        rel = self.store.release()
        if os.environ.get("VALLUM_PROVENANCE") == "merge-desk" and rel.get("slot") == "merged":
            rid, mr_iid = rel.get("release_id"), rel.get("mr_iid")
            self.store.set_release(slot="idle", candidate=None, step=None)
            self.audit(rid, {"by": "drive", "verdict": "nothing to deploy"}, finished_at=iso(), outcome="merged, no app change")
            self.store.event("merged", f"{rid} merged; no app change, nothing to deploy", release_id=rid)
            self.status(mr_iid, "Released", "skipped", note="no app change",
                        now="Merged. Nothing the app runs on changed, so there is nothing to test-drive.", action="nothing.")
            self.notify.ledger()
        log("no app change in this push: nothing to deploy")
        return 0

    def release(self) -> int:
        image = os.environ.get("IMAGE")
        if not image:
            return self.no_app_change()
        info = self.identify()
        rid, mr_iid, tier = info["release_id"], info.get("mr_iid"), info["tier"]
        names = sorted(set(info.get("features") or []) | set(info.get("touched") or []))
        steps = self.pol.steps(tier)
        watch = self.pol.watch_seconds(tier)
        total = len(steps) * 2 * watch + 600
        deadline = utcnow() + timedelta(seconds=total + 60 * float(self.pol["watchdog"]["deadlines_min"]["driving_extra"]))
        try:
            self.store.claim_release(("idle", "merged"), slot="driving", release_id=rid, mr_iid=mr_iid, tier=tier,
                                     features=info.get("features", []), touched=info.get("touched", []),
                                     feature=info.get("feature"), title=info.get("title"), author=info.get("author"),
                                     commit=info["commit"], deadline=iso(deadline), abort=False,
                                     drive_pipeline=os.environ.get("CI_PIPELINE_ID"))
        except Abort as exc:
            log(f"release slot busy: {exc}")
            return 1
        self.report.update(release=rid, mr=mr_iid, tier=tier, features=names, image=image, steps_planned=steps)
        log(f"{rid}: tier {tier}, steps {steps}, watch {watch:.0f}s, features {names or 'all'}")
        if info.get("owner_push"):
            self.audit(rid, {"by": "provenance", "result": "owner push (build phase override)"},
                       title=info.get("title"), feature=info.get("feature"), tier=tier, requester=info.get("author"),
                       started_at=iso(), human_actions=1)
        key = self.api_key()
        promises = self.promises(names)
        metric = [p for p in promises if p.style == "measure"]
        behave = [p for p in promises if p.style == "steps"]

        # -- deploy at 0% --------------------------------------------------------
        svc = self.cr.get()
        old_rev = self.store.release().get("last_good") or CloudRun.main_revision(svc)
        if old_rev and svc and old_rev not in CloudRun.serving(svc):
            old_rev = CloudRun.main_revision(svc)
        new_rev = f"{self.pol['cloud_run']['service']}-{info['commit'][:7]}-{os.environ.get('CI_PIPELINE_ID', '0')}"
        genesis = old_rev is None
        self.status(mr_iid, "Step 0", "running", note="0% of visitors", tier=tier, release_id=rid,
                    now=f"Deploying revision {new_rev} with no traffic.")
        traffic = [{"revisionName": new_rev, "percent": 100 if genesis else 0, "tag": "cand"}]
        if not genesis:
            traffic.append({"revisionName": old_rev, "percent": 100})
        env = {"GOOGLE_CLOUD_PROJECT": self.pol["cloud_run"]["project"]}
        try:
            svc = self.cr.deploy(image, new_rev, traffic, env)
            self.cr.ensure_public()
        except DeployFailed as exc:
            return self.refuse(info, new_rev, old_rev, f"the new revision didn't start: {exc}")
        cand_url = CloudRun.tag_url(svc, "cand") or self.url
        self.store.set_release(candidate=new_rev, old_revision=old_rev)
        log(f"deployed {new_rev} (old {old_rev}); private URL {cand_url}")

        # -- step 0: private URL, fresh test links, no visitor involved ---------------
        rows = []
        with core.client_for(cand_url, timeout=15) as client:
            for p in behave:
                out = core.run_step_promise(client, p, key, float(self.pol["judge"]["step_retry_s"]))
                rows.append({"id": p.id, "feature": p.feature, "type": p.type, "severity": p.severity, **out})
        fresh = ensure_links(cand_url, key, [], 10)
        needs = self.needs(metric)
        max_rps = float(self.pol["simulate"]["max_rps"])
        seconds = max(12.0, sum(n * 1.3 for r, n in needs.items() if r != "*") / (max_rps * 0.8))
        rates = plan_rates(needs, 1.0, seconds, 0, max_rps, {}) or {"GET /health": 20}
        v = Visitors(cand_url, fresh, key)
        asyncio.run(v.run(rates, seconds))
        warm = [s for s in v.samples if s.get("t", 0) >= 2.0]   # skip connection set-up in the first seconds
        for p in metric:
            m = parse_measure(p.data["measure"])
            got = core.judge_single(p, warm, int(needs.get(m.route, 1) * 0.8), revision=new_rev)
            rows.append({"id": p.id, "feature": p.feature, "type": p.type, "severity": p.severity, **got})
        bad = [r for r in rows if r["verdict"] in ("broken", "collecting") and judge.blocks(r["severity"], tier)]
        self.report["steps"].append({"step": 0, "url": cand_url, "visits": len(v.samples), "rows": rows,
                                     "verdict": "broken" if bad else "kept"})
        self.save_report(rid)
        if bad:
            worst = bad[0]
            return self.refuse(info, new_rev, old_rev, f"step 0: {worst['feature']}/{worst['id']} {worst['detail']}")
        self.audit(rid, {"by": "drive", "step": 0, "verdict": "kept", "visits": len(v.samples)})
        self.status(mr_iid, "Step 0", "done", note=f"{len(rows)} promises kept",
                    now="Step 0 passed on the private address.")
        self.store.event("step", f"{rid} step 0 kept ({len(rows)} promises)", release_id=rid)

        if genesis:
            return self.promote(info, new_rev, None, first=True)

        # -- traffic steps ----------------------------------------------------------------
        codes = self.sim_codes(key)
        for pct in steps:
            if pct >= 100:
                break
            self.store.set_release(step=pct)
            self.cr.set_traffic([{"revisionName": new_rev, "percent": pct, "tag": "cand"},
                                 {"revisionName": old_rev, "percent": 100 - pct}])
            self.status(mr_iid, f"{pct}%", "running", now=f"{pct}% of visitors are on the new version. Watching for {watch:.0f} s.")
            log(f"{pct}% on {new_rev}; watching {watch:.0f}s")
            rates = plan_rates(needs, pct / 100, watch - float(self.pol["judge"]["warmup_s"]),
                               float(self.pol["simulate"]["base_rps"]), float(self.pol["simulate"]["max_rps"]),
                               self.pol["simulate"]["mix"])
            verdict, rows, samples, elapsed = asyncio.run(self.watch(codes, key, rates, new_rev, old_rev, metric, tier, watch))
            per_rev = {}
            for s in samples:
                per_rev.setdefault(s.get("revision") or "none", 0)
                per_rev[s.get("revision") or "none"] += 1
            self.report["steps"].append({"step": pct, "verdict": verdict, "elapsed_s": round(elapsed, 1), "rates": rates,
                                         "visits": len(samples), "by_revision": per_rev, "rows": rows})
            self.save_report(rid)
            self.audit(rid, {"by": "drive", "step": pct, "verdict": verdict, "visits": len(samples)})
            if verdict != judge.KEPT:
                return self.rollback(info, new_rev, old_rev, pct, verdict, rows)
            self.status(mr_iid, f"{pct}%", "done", note=f"{len(samples)} visits")
            self.store.event("step", f"{rid} {pct}% kept", release_id=rid)
        return self.promote(info, new_rev, old_rev)

    async def watch(self, codes, key, rates, new_rev, old_rev, metric, tier, watch):
        v = Visitors(self.url, codes, key)
        stop = asyncio.Event()
        runner = asyncio.create_task(v.run(rates, 2 * watch + 5, stop))
        tick = float(self.pol["judge"]["tick_s"])
        start = time.monotonic()
        verdict, rows = judge.COLLECTING, []
        while True:
            await asyncio.sleep(tick)
            elapsed = time.monotonic() - start
            if await asyncio.to_thread(self.aborted):
                verdict = "aborted"
                break
            final = elapsed >= watch
            rows = [judge.judge_promise(p, list(v.samples), new_rev, old_rev,
                                        need=int(p.data.get("judge", {}).get("min_samples", self.pol.min_samples(parse_measure(p.data["measure"]).kind))),
                                        worse=float(self.pol["judge"]["worse_than_old"]),
                                        margin=float(self.pol["judge"]["at_risk_margin"]), final=final,
                                        warmup_s=float(self.pol["judge"]["warmup_s"])) for p in metric]
            verdict = judge.overall(rows, tier, elapsed, watch, final)
            w = judge.worst(rows)
            log(f"  {elapsed:5.0f}s {verdict:10} visits={len(v.samples)}"
                + (f" worst={w['id']}: {w['detail']}" if w else ""))
            if verdict in (judge.BROKEN, judge.AT_RISK) or (verdict == judge.KEPT and final):
                break
        stop.set()
        await runner
        return verdict, rows, v.samples, time.monotonic() - start

    # ---------------------------------------------------------------- outcomes
    def promote(self, info, new_rev, old_rev, first=False) -> int:
        rid, mr_iid = info["release_id"], info.get("mr_iid")
        self.cr.set_traffic([{"revisionName": new_rev, "percent": 100, "tag": "cand"}])
        rel = self.store.release()
        history = ([old_rev] if old_rev else []) + [r for r in rel.get("previous_good", []) if r != old_rev]
        self.store.set_release(slot="idle", last_good=new_rev, previous_good=history[:5], candidate=None, step=None,
                               abort=False, released_at=iso(), release_id=rid)
        self.audit(rid, {"by": "drive", "step": 100, "verdict": "promoted"}, finished_at=iso(), outcome="released",
                   revision=new_rev, owner_notified=bool(mr_iid))
        self.store.event("released", f"{rid} {info.get('feature')} released" + (" (first deploy)" if first else ""),
                         release_id=rid)
        kept = sorted({f"{r.get('feature')}/{r.get('id')}" for st in self.report["steps"] for r in st.get("rows", [])
                       if r.get("verdict") == "kept"})
        self.gitlab_release(info, new_rev, kept)
        issue_iid = self.issue_of(mr_iid)
        if mr_iid and not first:
            started = None
            if issue_iid:
                try:
                    started = self.gl.get(f"/projects/:id/issues/{issue_iid}").get("created_at")
                except Exception:
                    started = None
            minutes = round((utcnow() - config.parse_iso(started)).total_seconds() / 60, 1) if started else None
            server = os.environ.get("CI_SERVER_URL", "https://gitlab.com")
            self.start_agent("release", {
                "release_id": rid, "title": info.get("title"), "feature": info.get("feature"), "tier": info.get("tier"),
                "revision": new_rev, "merge_request_iid": mr_iid, "issue_iid": issue_iid,
                "merge_request_url": f"{server}/{os.environ.get('CI_PROJECT_PATH', '')}/-/merge_requests/{mr_iid}",
                "pipeline_url": os.environ.get("CI_PIPELINE_URL"), "promises_kept": kept,
                "test_drive": ["step 0"] + [f"{s['step']}%" for s in self.report["steps"] if s["step"]] + ["100%"],
                "issue_to_live_minutes": minutes, "human_actions": 0}, issue_id=issue_iid)
        self.status(mr_iid, "Released 100%", "done", note=new_rev,
                    now=f"Live for everyone. [Open the app]({self.url}/status).", action="nothing.")
        if mr_iid:
            self.notify.alert(MR, int(mr_iid), "Released", author=info.get("author"),
                              what=f"{info.get('title')} is live for 100% of visitors ({rid}, revision `{new_rev}`).",
                              impact="None: every promise was kept at every step.",
                              done="Step 0 and each traffic step passed; Vallum promoted the new revision.",
                              nxt="The Live Patrol keeps checking its promises in production.", action="No.")
        self.notify.ledger()
        log(f"{rid} released: {new_rev}")
        return 0

    def refuse(self, info, new_rev, old_rev, why: str) -> int:
        """Failed before any visitor saw it: nothing to undo, nothing spent."""
        rid, mr_iid = info["release_id"], info.get("mr_iid")
        if old_rev:
            try:
                self.cr.set_traffic([{"revisionName": old_rev, "percent": 100}])
            except Exception as exc:
                log(f"could not reset traffic: {exc}")
        self.store.set_release(slot="idle", candidate=None, step=None, abort=False)
        self.audit(rid, {"by": "drive", "step": 0, "verdict": "refused", "why": why}, finished_at=iso(), outcome="refused at step 0")
        self.store.event("refused", f"{rid} refused at step 0: {why[:120]}", release_id=rid)
        self.status(mr_iid, "Step 0", "failed", note="no visitor affected", now=f"Not released: {why}",
                    action="nothing yet; the fix goes through a new MR.")
        self.notify.alert(MR, mr_iid, "Release refused at step 0", author=info.get("author"),
                          what=f"{info.get('title')} ({rid}) failed a promise on its private address: {why}",
                          impact="None. No visitor was ever sent to the new version and no allowance was spent.",
                          done=f"Kept 100% of traffic on {old_rev or 'the current revision'}.",
                          nxt="The change stays merged in main but not live. A fix MR will release on top of it.",
                          action="Only if you want to revert the merge now.")
        self.notify.ledger()
        self.save_report(rid)
        return 1

    def rollback(self, info, new_rev, old_rev, pct, verdict, rows) -> int:
        rid, mr_iid = info["release_id"], info.get("mr_iid")
        t0 = time.monotonic()
        if verdict != "aborted":
            self.cr.set_traffic([{"revisionName": old_rev, "percent": 100}, {"revisionName": new_rev, "percent": 0, "tag": "cand"}])
        took = time.monotonic() - t0
        tripped = self.store.record_rollback(float(self.pol["circuit_breaker"]["within_hours"]),
                                             int(self.pol["circuit_breaker"]["rollbacks"]))
        self.store.set_release(slot="idle", candidate=None, step=None, abort=False, last_good=old_rev)
        w = judge.worst(rows) or {}
        reason = "the owner ran rollback-now" if verdict == "aborted" else f"{w.get('feature')}/{w.get('id')} {verdict}: {w.get('detail')}"
        self.audit(rid, {"by": "drive", "step": pct, "verdict": "rolled back", "why": reason, "seconds": round(took, 1)},
                   finished_at=iso(), outcome="rolled back")
        incident = None
        if verdict != "aborted":
            try:
                body = (f"🛡 Vallum: release `{rid}` was rolled back at the {pct}% step.\n\n"
                        f"**Broken promise:** {reason}\n\n**Revision:** `{new_rev}` → back to `{old_rev}` in {took:.1f} s\n\n"
                        f"**Change:** {('!' + str(mr_iid)) if mr_iid else info.get('commit', '')[:8]} {info.get('title')}\n\n"
                        "```json\n" + json.dumps(rows, indent=1, default=str)[:3000] + "\n```\n\n"
                        "_Layer 2: the Incident Commander adds the postmortem and a learned promise here._")
                incident = self.gl.create_issue(f"Incident: {rid} rolled back ({w.get('id', verdict)} at {pct}%)", body,
                                                [self.pol.label("incident")])
            except Exception as exc:
                log(f"could not open the incident issue: {exc}")
        self.store.event("rolled_back", f"{rid} rolled back at {pct}%: {reason[:120]}", release_id=rid,
                         incident=incident and incident["iid"])
        if incident:
            broken = [r for r in rows if r.get("verdict") in (judge.BROKEN, judge.AT_RISK)][:5]
            self.start_agent("incident", {
                "release_id": rid, "incident_iid": incident["iid"], "step_percent": pct, "verdict": verdict,
                "broken_promises": broken, "merge_request_iid": mr_iid, "issue_iid": self.issue_of(mr_iid),
                "bad_revision": new_rev, "good_revision": old_rev, "rollback_seconds": round(took, 1),
                "tier": info.get("tier"), "feature": info.get("feature"), "title": info.get("title")},
                issue_id=incident["iid"])
        self.status(mr_iid, f"{pct}%", "failed", note=w.get("id", verdict))
        self.status(mr_iid, "Rolled back", "rolled_back", note=f"{took:.0f} s",
                    now=f"Rolled back: {reason}." + (f" Incident #{incident['iid']} is open." if incident else ""),
                    action="nothing. A fix will come through a new MR.")
        self.notify.alert(MR, mr_iid, "Rolled back", author=info.get("author"),
                          what=f"{info.get('title')} ({rid}) was rolled back at the {pct}% step: {reason}.",
                          impact=f"{pct}% of visitors were on the new version until the rollback.",
                          done=f"Sent 100% of traffic back to `{old_rev}` in {took:.1f} s."
                               + (f" Opened incident #{incident['iid']}." if incident else ""),
                          nxt=("Safe mode is now ON (circuit breaker). " if tripped else "")
                              + "The change stays merged but not live; a fix MR will release on top of it.",
                          action="Yes: say @vallum resume when ready." if tripped else "No.")
        self.notify.ledger()
        self.save_report(rid)
        log(f"{rid} rolled back to {old_rev} in {took:.1f}s ({reason})")
        return 1


def rollback_now() -> int:
    """The owner's emergency button: run the manual job `rollback-now` in any main pipeline."""
    pol = config.policy()
    gl = GitLab.from_env(require_token=True)
    store = Store.from_env()
    notify = Notifier(gl, store, pol)
    cr_cfg = pol["cloud_run"]
    cr = CloudRun(gcp.session(), cr_cfg["project"], cr_cfg["region"], cr_cfg["service"])
    rel = store.release()
    svc = cr.get()
    serving = CloudRun.main_revision(svc)
    if rel.get("slot") == "driving" and rel.get("old_revision"):
        target, undone = rel["old_revision"], rel.get("candidate")
        store.set_release(abort=True)
    else:
        history = [r for r in rel.get("previous_good", []) if r != serving]
        if not history:
            print("Nothing to roll back to: no earlier good revision is recorded.")
            return 1
        target, undone = history[0], serving
        store.set_release(last_good=target, previous_good=history[1:])
    t0 = time.monotonic()
    cr.set_traffic([{"revisionName": target, "percent": 100}])
    took = time.monotonic() - t0
    who = os.environ.get("GITLAB_USER_LOGIN", "someone")
    rid = rel.get("release_id") or "manual"
    record = store.load_release(rid) or {"release": rid, "decisions": []}
    record.setdefault("decisions", []).append({"by": f"rollback-now (@{who})", "verdict": "rolled back",
                                               "from": undone, "to": target, "seconds": round(took, 1), "at": iso()})
    record["human_actions"] = int(record.get("human_actions", 0)) + 1
    store.save_release(rid, record)
    store.record_rollback(float(pol["circuit_breaker"]["within_hours"]), int(pol["circuit_breaker"]["rollbacks"]))
    store.event("rolled_back", f"rollback-now by @{who}: {undone} → {target} in {took:.1f}s", release_id=rid)
    mr_iid = rel.get("mr_iid")
    if mr_iid:
        notify.status(MR, int(mr_iid), "Rolled back", "rolled_back", note=f"by @{who}",
                      now=f"@{who} ran rollback-now. Traffic is back on `{target}`.", action="nothing.")
    notify.alert(MR, mr_iid, "Rolled back by hand", author=rel.get("author"),
                 what=f"@{who} ran the rollback-now job.", impact=f"`{undone}` no longer serves any visitors.",
                 done=f"Sent 100% of traffic to `{target}` in {took:.1f} s. Recorded as an override.",
                 nxt="The code stays in main. The next release will include it unless it is reverted.", action="No.")
    notify.ledger()
    print(f"Rolled back {undone} → {target} in {took:.1f}s")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "release"
    if cmd == "release":
        sys.exit(Drive().release())
    if cmd == "rollback-now":
        sys.exit(rollback_now())
    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
