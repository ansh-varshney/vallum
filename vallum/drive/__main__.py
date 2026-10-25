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
from vallum import agents, allowance, config, evidence, gcp, housekeeping
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


def step0_seconds(seconds: float, rates: dict, star_need: int, warmup_s: float = 2.0) -> float:
    """Long enough for app-wide promises (route '*') too: r-0007 was refused with 109 of 160 samples because
    step 0 only planned for the feature's own routes."""
    total = sum(rates.values()) or 1.0
    if star_need and total * max(seconds - warmup_s, 1.0) < star_need * 1.3:
        return star_need * 1.3 / total + warmup_s
    return seconds


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

    def promise_need(self, p: cardlib.Promise) -> int:
        m = parse_measure(p.data["measure"])
        return int(p.data.get("judge", {}).get("min_samples", self.pol.min_samples(m.kind)))

    def needs(self, metric: list[cardlib.Promise]) -> dict:
        """Visits to plan per route. A filtered promise (for example popular == 1) only sees part of a route's
        visits, so it is planned with twice its need (seen live: 136 of 160 popular samples at step 0)."""
        out = {}
        for p in metric:
            m = parse_measure(p.data["measure"])
            need = self.promise_need(p) * (2 if m.filters else 1)
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

    def start_agent(self, key: str, kind: str, iid: int, goal: dict, issue_id=None) -> str | None:
        """Agents think after code acts: a failure here never changes the release outcome. Started by assigning
        the flow's bot (member-only action, namespace credits); the Flows API is the fallback."""
        try:
            via = agents.start(Flows(self.gl, self.store), self.store, self.pol, self.notify, key, kind, int(iid), goal,
                               as_reviewer=False, issue_id=issue_id)
        except Exception as exc:
            log(f"could not start the {key} flow: {exc}")
            return None
        if via:
            self.store.event("agent", f"{goal.get('release_id')}: started {key} ({via})")
            log(f"started {key} flow ({via}) on {kind}/{iid}")
        return via

    def wait_serving(self, revision: str, streak: int = 15, timeout: float = 90) -> bool:
        """The rollback is done when visitors get the old revision: 15 answers in a row from it."""
        import httpx

        end, seen = time.monotonic() + timeout, 0
        while time.monotonic() < end:
            try:
                rev = httpx.get(self.url + "/health", timeout=5).headers.get("x-revision")
            except Exception:
                rev = None
            seen = seen + 1 if rev == revision else 0
            if seen >= streak:
                return True
            time.sleep(0.3)
        log(f"could not confirm {revision} serves every visitor within {timeout:.0f}s")
        return False

    def spend(self, samples: list[dict], label: str, new_rev: str | None = None):
        """Every simulated visit during a traffic step is a visit; the bad ones the new revision served spend
        the mistake allowance."""
        tier_of, limits = allowance.route_profile(self.cards.cards)
        counts = allowance.count(samples, tier_of, limits, float(self.pol.get("allowance", {}).get("slow_factor", 2.0)),
                                 warmup_s=float(self.pol["judge"]["warmup_s"]), blame=new_rev)
        allowance.record(self.store, counts, label, None, self.pol.window_minutes())
        try:
            # modes follow at once (a slip can turn a tier Red mid-release), not at the next Live Patrol
            changes = allowance.update_modes(self.store, self.pol)
            allowance.announce(changes, self.store, self.notify, self.pol.owners[0] if self.pol.owners else None)
            if changes:
                log("modes: " + ", ".join(f"{c['tier']} {c['from']}→{c['to']}" for c in changes))
        except Exception as exc:
            log(f"could not update the modes: {exc}")
        return counts

    def clear_labels(self, mr_iid, issue_iid):
        """A released change carries no stale hold labels (needs-human, paused, blocked)."""
        stale = [self.pol.label(k) for k in ("needs_human", "paused", "blocked", "ready")]
        try:
            if mr_iid:
                self.gl.mr_labels(int(mr_iid), remove=stale)
            if issue_iid:
                self.gl.edit_issue(int(issue_iid), remove_labels=",".join(stale))
        except Exception as exc:
            log(f"could not clear old labels: {exc}")

    def carried_requests(self, carried: list[str], rid: str) -> list[dict]:
        """Earlier releases of the carried features that never reached visitors (refused or rolled back), with
        their merge requests: those requests go live now, with this release."""
        out, seen = [], set()
        for _key, rec in self.store.b.list("vallum_releases"):
            outcome = str(rec.get("outcome") or "")
            mr = rec.get("mr")
            if rec.get("release") == rid or not mr or mr in seen or not outcome.startswith(("refused", "rolled back")):
                continue
            if rec.get("released_in") or not any(f in str(rec.get("feature", "")) for f in carried):
                continue
            seen.add(mr)
            out.append(rec)
        return out

    def release_carried(self, info: dict, rid: str, new_rev: str):
        """Requests whose own release was refused or rolled back went live with this one: tell their owners and
        close their issues (code, no agent). Seen live: the link preview (#8) shipped together with the fix (#6)."""
        carried = info.get("carried") or []
        if not carried:
            return
        for rec in self.carried_requests(carried, rid):
            mr = int(rec["mr"])
            issue = self.issue_of(mr)
            note = (f"🛡 Vallum: **Released** · {rec.get('title')} went live with `{rid}` (revision `{new_rev}`), "
                    f"after its own release `{rec.get('release')}` did not reach visitors ({rec.get('outcome')}). "
                    "Its promises were judged again in this test drive.\n\n_Written by code from the release records._")
            try:
                if issue:
                    self.gl.add_note("issues", issue, note)
                    self.gl.edit_issue(issue, state_event="close",
                                       remove_labels=",".join(self.pol.label(k) for k in ("needs_human", "paused", "blocked")))
                self.gl.add_note(MR, mr, note)
                rec["released_in"] = rid
                self.store.save_release(rec.get("release"), rec)
                self.store.event("released", f"!{mr} {rec.get('title')} went live with {rid}", release_id=rid)
                log(f"carried request !{mr} (issue #{issue}) went live with {rid}")
            except Exception as exc:
                log(f"could not report the carried request !{mr}: {exc}")

    def housekeeping(self, info: dict):
        """Refresh the code map and drop the watch promises that expired with this release (one docs-only MR)."""
        names = set(info.get("features") or []) | set(info.get("touched") or [])
        paths = [self.cards.paths[n] for n in names if n in self.cards.paths]
        try:
            actions = housekeeping.changes(config.ROOT, paths, lambda p: self.gl.file_raw_or_none(p, "main"))
            mr = housekeeping.open_mr(self.gl, self.pol, info["release_id"], actions)
            if mr:
                self.store.event("housekeeping", f"{info['release_id']}: opened !{mr['iid']} ({len(actions)} file(s))")
                log(f"housekeeping MR !{mr['iid']}: " + ", ".join(a["file_path"] for a in actions))
            else:
                log("housekeeping: code map current, no expired watch promises")
        except Exception as exc:
            log(f"housekeeping failed (release unaffected): {exc}")

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

    def widen(self, info: dict) -> dict:
        """Judge everything that changed since the live code, not only this merge request: a change that was
        rolled back stays merged in main, and the next release ships it again (seen with r-0006 → r-0007)."""
        live = self.store.live_commit(self.pol["cloud_run"]["service"])
        if not live:
            return info
        try:
            files = [d["new_path"] for d in self.gl.compare(live, info["commit"]).get("diffs", [])]
        except Exception as exc:
            log(f"could not compare with the live code ({live[:8]}): {exc}")
            return info
        touched = set(cardlib.blast_radius(files, self.cards.cards))
        changed = {n for n, p in self.cards.paths.items() if p in files}
        known = set(info.get("features") or []) | set(info.get("touched") or [])
        extra = sorted((touched | changed) - known)
        if not extra:
            return info
        names = sorted(set(info.get("features") or []) | changed)
        all_touched = sorted(set(info.get("touched") or []) | touched)
        subjects = [self.cards.cards[n] for n in set(names) | set(all_touched) if n in self.cards.cards]
        tier = cardlib.effective_tier(subjects, config.catalog(), set(all_touched), self.cards.cards, files,
                                      self.pol.get("core_paths", []))
        if cardlib.TIER_RANK.get(info.get("tier"), 0) > cardlib.TIER_RANK.get(tier, 0):
            tier = info["tier"]
        log(f"also judging {', '.join(extra)}: changed since the live code ({live[:8]}), tier {tier}")
        return {**info, "features": names, "touched": all_touched, "tier": tier, "carried": extra}

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
        info = self.widen(self.identify())
        rid, mr_iid, tier = info["release_id"], info.get("mr_iid"), info["tier"]
        names = sorted(set(info.get("features") or []) | set(info.get("touched") or []))
        mode = ((self.store.doc("modes").get(tier) or {}).get("mode")) or "green"
        steps = self.pol.steps(tier, mode)
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
        self.report.update(release=rid, mr=mr_iid, tier=tier, mode=mode, features=names, image=image, steps_planned=steps)
        log(f"{rid}: tier {tier} ({mode.upper()}), steps {steps}, watch {watch:.0f}s, features {names or 'all'}")
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
        replay = [p.id for p in promises if p.data.get("replay")]
        if replay:
            # a learned promise asks step 0 to replay real traffic (real links, real popularity), not only test links
            sim = self.pol["simulate"]
            rates = plan_rates(needs, 1.0, seconds, float(sim["base_rps"]), max_rps, sim["mix"])
            v = Visitors(cand_url, self.sim_codes(key) + fresh, key)
            log(f"step 0 replays real traffic for {', '.join(replay)}")
        else:
            rates = plan_rates(needs, 1.0, seconds, 0, max_rps, {}) or {"GET /health": 20}
            v = Visitors(cand_url, fresh, key)
        seconds = step0_seconds(seconds, rates, needs.get("*", 0))
        asyncio.run(v.run(rates, seconds))
        warm = [s for s in v.samples if s.get("t", 0) >= 2.0]   # skip connection set-up in the first seconds
        for p in metric:
            got = core.judge_single(p, warm, int(self.promise_need(p) * 0.8), revision=new_rev)
            rows.append({"id": p.id, "feature": p.feature, "type": p.type, "severity": p.severity, **got})
        bad = [r for r in rows if r["verdict"] in ("broken", "collecting") and judge.blocks(r["severity"], tier)]
        self.report["steps"].append({"step": 0, "url": cand_url, "visits": len(v.samples), "rows": rows,
                                     "replay": replay, "verdict": "broken" if bad else "kept"})
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
            # at a first step under 10% a promise may not collect its sample; it is judged again at the next step
            carry = pct < 10 and pct != steps[-1]
            verdict, rows, samples, elapsed = asyncio.run(self.watch(codes, key, rates, new_rev, old_rev, metric, tier,
                                                                     watch, carry))
            per_rev = {}
            for s in samples:
                per_rev.setdefault(s.get("revision") or "none", 0)
                per_rev[s.get("revision") or "none"] += 1
            counts = self.spend(samples, f"drive {rid} {pct}%", new_rev)
            self.report["steps"].append({"step": pct, "verdict": verdict, "elapsed_s": round(elapsed, 1), "rates": rates,
                                         "visits": len(samples), "by_revision": per_rev, "rows": rows,
                                         "allowance_counts": counts})
            self.save_report(rid)
            self.audit(rid, {"by": "drive", "step": pct, "verdict": verdict, "visits": len(samples)})
            if verdict != judge.KEPT:
                return self.rollback(info, new_rev, old_rev, pct, verdict, rows)
            self.status(mr_iid, f"{pct}%", "done", note=f"{len(samples)} visits")
            self.store.event("step", f"{rid} {pct}% kept", release_id=rid)
        return self.promote(info, new_rev, old_rev)

    async def watch(self, codes, key, rates, new_rev, old_rev, metric, tier, watch, carry=False):
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
            verdict = judge.overall(rows, tier, elapsed, watch, final, carry=carry)
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
                               abort=False, released_at=iso(), release_id=rid, promoted_revision=new_rev,
                               promoted_commit=info.get("commit"))
        self.audit(rid, {"by": "drive", "step": 100, "verdict": "promoted"}, finished_at=iso(), outcome="released",
                   revision=new_rev, previous_revision=old_rev, feature=info.get("feature"), owner_notified=bool(mr_iid))
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
            record = {
                "release_id": rid, "title": info.get("title"), "feature": info.get("feature"), "tier": info.get("tier"),
                "mode": self.report.get("mode"), "revision": new_rev, "previous_revision": old_rev,
                "merge_request_iid": mr_iid, "issue_iid": issue_iid,
                "merge_request_url": f"{server}/{os.environ.get('CI_PROJECT_PATH', '')}/-/merge_requests/{mr_iid}",
                "pipeline_url": os.environ.get("CI_PIPELINE_URL"), "promises_kept": kept,
                "test_drive": ["step 0"] + [f"{s['step']}%" for s in self.report["steps"] if s["step"]] + ["100%"],
                "issue_to_live_minutes": minutes,
                "human_actions": int((self.store.load_release(rid) or {}).get("human_actions", 0))}
            kind, target = ("issues", issue_iid) if issue_iid else (MR, mr_iid)
            try:
                self.gl.add_note(kind, int(target), "🛡 Vallum: **Release record** (written by code; the Release Captain "
                                 "summarises it)\n\n```json\n" + json.dumps(record, indent=1, default=str) + "\n```")
            except Exception as exc:
                log(f"could not post the release record: {exc}")
            self.start_agent("release", kind, target, record, issue_id=issue_iid)
        self.clear_labels(mr_iid, issue_iid)
        self.release_carried(info, rid, new_rev)
        self.status(mr_iid, "Released 100%", "done", note=new_rev,
                    now=f"Live for everyone. [Open the app]({self.url}/status).", action="nothing.")
        if mr_iid:
            self.notify.alert(MR, int(mr_iid), "Released", author=info.get("author"),
                              what=f"{info.get('title')} is live for 100% of visitors ({rid}, revision `{new_rev}`).",
                              impact="None: every promise was kept at every step.",
                              done="Step 0 and each traffic step passed; Vallum promoted the new revision.",
                              nxt="The Live Patrol keeps checking its promises in production.", action="No.")
        if not info.get("housekeeping"):
            self.housekeeping(info)
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
            self.wait_serving(old_rev)
        took = time.monotonic() - t0
        tripped = self.store.record_rollback(float(self.pol["circuit_breaker"]["within_hours"]),
                                             int(self.pol["circuit_breaker"]["rollbacks"]))
        self.store.set_release(slot="idle", candidate=None, step=None, abort=False, last_good=old_rev)
        w = judge.worst(rows) or {}
        reason = "the owner ran rollback-now" if verdict == "aborted" else f"{w.get('feature')}/{w.get('id')} {verdict}: {w.get('detail')}"
        self.audit(rid, {"by": "drive", "step": pct, "verdict": "rolled back", "why": reason, "seconds": round(took, 1)},
                   finished_at=iso(), outcome="rolled back")
        incident = None
        data_only = verdict == judge.AT_RISK and (w.get("verdict") == judge.COLLECTING)
        if data_only:
            log("rolled back for lack of data, not a broken promise: no incident")
        if verdict != "aborted" and not data_only:
            try:
                broken = [r for r in rows if r.get("verdict") in (judge.BROKEN, judge.AT_RISK)][:5]
                record = {"release_id": rid, "step_percent": pct, "verdict": verdict, "broken_promises": broken,
                          "merge_request_iid": mr_iid, "issue_iid": self.issue_of(mr_iid), "bad_revision": new_rev,
                          "good_revision": old_rev, "rollback_seconds": round(took, 1), "tier": info.get("tier"),
                          "feature": info.get("feature"), "title": info.get("title")}
                body = (f"🛡 Vallum: release `{rid}` was rolled back at the {pct}% step.\n\n"
                        f"**Broken promise:** {reason}\n\n**Revision:** `{new_rev}` → back to `{old_rev}` in {took:.1f} s\n\n"
                        f"**Change:** {('!' + str(mr_iid)) if mr_iid else info.get('commit', '')[:8]} {info.get('title')}\n\n"
                        "**Incident record** (written by code; the Incident Commander reads it)\n"
                        "```json\n" + json.dumps(record, indent=1, default=str)[:6000] + "\n```")
                incident = self.gl.create_issue(f"Incident: {rid} rolled back ({w.get('id', verdict)} at {pct}%)", body,
                                                [self.pol.label("incident")])
            except Exception as exc:
                log(f"could not open the incident issue: {exc}")
        self.store.event("rolled_back", f"{rid} rolled back at {pct}%: {reason[:120]}", release_id=rid,
                         incident=incident and incident["iid"])
        if incident:
            self.start_agent("incident", "issues", incident["iid"], {"incident_iid": incident["iid"], **record},
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
    tripped = store.record_rollback(float(pol["circuit_breaker"]["within_hours"]), int(pol["circuit_breaker"]["rollbacks"]))
    store.event("rolled_back", f"rollback-now by @{who}: {undone} → {target} in {took:.1f}s", release_id=rid)
    if tripped:
        meta = store.doc("meta")
        store.event("safe-mode", f"circuit breaker: {meta.get('safe_mode_reason')}")
        notify.alert("issues", None, "Safe mode on (circuit breaker)", author=pol.owners[0] if pol.owners else None,
                     what=f"{meta.get('safe_mode_reason')}.",
                     impact="No new merges or releases until a member resumes.",
                     done="Safe mode is on. Traffic stays where it is.",
                     nxt="Find out why releases keep failing, then say `@vallum resume`.",
                     action="Yes: say `@vallum resume` when ready.")
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
