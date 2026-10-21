"""The Merge Desk (B17): the only thing that merges. Runs on main with resource_group: release,
on a schedule and at the end of every main pipeline. One release at a time.

For the next ready MR it: rebases onto main, waits for a fresh pipeline on that exact commit,
runs all 12 Gatekeeper rules, records the commit as sanctioned for the provenance gate,
gives the owner's approval (the MR approval rule needs it) and merges pinned to that SHA.

    python -m vallum.desk
"""

import sys
import time
from datetime import timedelta

from vallum import config, evidence, priority, verdicts
from vallum.config import iso, parse_iso, utcnow
from vallum.flows import Flows
from vallum.gate import context, rules
from vallum.gitlab import GitLab, GitLabError
from vallum.notify import Notifier
from vallum.state import Abort, Store

MR = "merge_requests"
FINISHED = {"success", "failed", "canceled", "skipped"}


class Desk:
    def __init__(self, gl: GitLab, store: Store, pol):
        self.gl = gl
        self.store = store
        self.pol = pol
        self.notify = Notifier(gl, store, pol)
        self.flows = Flows(gl, store)

    def log(self, msg: str):
        print(f"[merge-desk {utcnow().strftime('%H:%M:%S')}] {msg}", flush=True)

    # ------------------------------------------------------------------ queue
    def classify(self, mrs: list[dict]):
        ready, waiting, blocked = [], [], []
        for mr in mrs:
            if mr.get("draft") or mr.get("work_in_progress"):
                continue
            full = self.gl.mr(mr["iid"])
            hp = full.get("head_pipeline") or {}
            if hp.get("sha") == full["sha"] and hp.get("status") == "success":
                ready.append(full)
            elif hp.get("sha") == full["sha"] and hp.get("status") in FINISHED:
                blocked.append(full)
            elif full.get("diverged_commits_count", 0) and hp.get("status") == "success":
                ready.append(full)  # passed before main moved on; the desk rebases it
            else:
                waiting.append(full)
        return ready, waiting, blocked

    def level(self, mr: dict) -> str:
        level = priority.label_override(mr.get("labels")) or "P2"
        return priority.aged(level, mr.get("created_at"))

    def save_queue(self, ready, waiting, blocked):
        items = [f"!{m['iid']} {m['title']} · {self.level(m)} · ready" for m in ready]
        items += [f"!{m['iid']} {m['title']} · waiting for its pipeline" for m in waiting]
        items += [f"!{m['iid']} {m['title']} · blocked: pipeline failed" for m in blocked]
        self.store.update("queue", lambda _cur: {"items": items, "at": iso()})

    # ------------------------------------------------------------------ steps
    def rebase_and_wait(self, mr: dict) -> dict | None:
        iid = mr["iid"]
        if mr.get("diverged_commits_count", 0) > 0 or mr.get("detailed_merge_status") == "need_rebase":
            self.log(f"!{iid}: rebasing onto main")
            self.notify.status(MR, iid, "Rebased on main", "running")
            self.gl.put(f"/projects/:id/merge_requests/{iid}/rebase")
            deadline = time.monotonic() + float(self.pol["merge_desk"]["rebase_wait_s"])
            while True:
                time.sleep(3)
                mr = self.gl.mr(iid)
                if not mr.get("rebase_in_progress"):
                    break
                if time.monotonic() > deadline:
                    self.log(f"!{iid}: rebase timed out")
                    return None
            if mr.get("merge_error"):
                self.log(f"!{iid}: rebase failed: {mr['merge_error']}")
                self.notify.status(MR, iid, "Rebased on main", "failed", note="conflict",
                                   now="The rebase hit a conflict. Layer 2 sends this to the Pipeline Medic; for now a person must rebase.",
                                   action="rebase this branch onto main.")
                self.gl.mr_labels(iid, add=[self.pol.label("blocked")], remove=[self.pol.label("ready")])
                return None
            self.notify.status(MR, iid, "Rebased on main", "done")
        sha = mr["sha"]
        self.notify.status(MR, iid, "Fresh pipeline", "running", note=sha[:8],
                           now=f"Waiting for the pipeline on {sha[:8]} before the full check.")
        deadline = time.monotonic() + float(self.pol["merge_desk"]["pipeline_wait_min"]) * 60
        while True:
            mr = self.gl.mr(iid)
            hp = mr.get("head_pipeline") or {}
            if mr["sha"] != sha:
                self.log(f"!{iid}: new commits arrived while waiting; leaving it for the next run")
                return None
            if hp.get("sha") == sha and hp.get("status") in FINISHED:
                break
            if time.monotonic() > deadline:
                self.log(f"!{iid}: pipeline still not finished; next run will look again")
                return None
            time.sleep(10)
        ok = hp.get("status") == "success"
        self.notify.status(MR, iid, "Fresh pipeline", "done" if ok else "failed", note=f"{sha[:8]} {hp.get('status')}")
        return mr if ok else None

    def approve(self, iid: int, sha: str):
        approvals = self.gl.get(f"/projects/:id/merge_requests/{iid}/approvals")
        if any(a["user"]["username"] in self.pol.owners for a in approvals.get("approved_by", [])):
            return
        try:
            self.gl.post(f"/projects/:id/merge_requests/{iid}/approve", json={"sha": sha})
        except GitLabError as exc:
            if exc.status not in (401, 409):  # already approved
                raise

    def merge(self, iid: int, sha: str) -> dict:
        last = None
        for _ in range(8):
            try:
                return self.gl.put(f"/projects/:id/merge_requests/{iid}/merge",
                                   json={"sha": sha, "should_remove_source_branch": True})
            except GitLabError as exc:
                last = exc
                if exc.status in (405, 406, 409, 422):
                    time.sleep(5)
                    continue
                raise
        raise last

    # ------------------------------------------------------------------ agents (Layer 2)
    def runs_left(self, tl: dict) -> int:
        cap = int(self.pol["releases"]["max_flow_runs_per_issue"])
        return cap - int(tl.get("flow_runs", 0))

    def kick(self, mr: dict, kind: str, key: str, goal, fp: str) -> bool:
        """Start a flow for this MR unless one is already running for the same change."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        sess = (tl.get("sessions") or {}).get(kind) or {}
        started = parse_iso(sess.get("at"))
        wait_min = float(self.pol["watchdog"]["deadlines_min"].get("reviewing", 15))
        if sess.get("fp") == fp and started and utcnow() - started < timedelta(minutes=wait_min):
            self.log(f"!{mr['iid']}: {kind} session {sess.get('id')} still running")
            return False
        if self.runs_left(tl) <= 0:
            self.log(f"!{mr['iid']}: flow-run cap reached")
            self.store.set_timeline(target, refused_sha=mr["sha"])
            self.gl.mr_labels(mr["iid"], add=[self.pol.label("needs_human")])
            self.notify.status(MR, mr["iid"], kind.capitalize(), "failed", note="cap reached",
                               now=f"Vallum ran {self.pol['releases']['max_flow_runs_per_issue']} agent sessions on this without a verdict.",
                               action="look at the sessions and decide (label vallum::needs-human).")
            return False
        try:
            started = self.flows.start(key, goal)
        except Exception as exc:
            self.log(f"!{mr['iid']}: could not start {kind}: {exc}")
            return False
        sessions = dict(tl.get("sessions") or {})
        sessions[kind] = {"id": started.get("id"), "fp": fp, "at": iso(), "sha": mr["sha"]}
        self.store.set_timeline(target, sessions=sessions, flow_runs=int(tl.get("flow_runs", 0)) + 1)
        self.store.event("agent", f"!{mr['iid']}: started {kind} session {started.get('id')}", mr_iid=mr["iid"])
        self.log(f"!{mr['iid']}: started {kind} session {started.get('id')}")
        return True

    def agent_verdicts(self, mr: dict) -> dict | None:
        """Make sure the Reviewer and Inspector have spoken on this change; start them if not.
        Returns the verdicts when both exist, None while waiting (FR-INSP-4: a new change needs a new inspection)."""
        if not self.pol.get("gate", {}).get("agent_verdicts_required"):
            return {}
        iid = mr["iid"]
        found = verdicts.latest(self.gl.notes(MR, iid), self.flows.bots())
        fp = evidence.fingerprint(self.gl.mr_changes(iid))
        if not found.get("review"):
            age = utcnow() - (parse_iso(mr.get("created_at")) or utcnow())
            if age < timedelta(minutes=8):
                self.log(f"!{iid}: young MR; giving the Ship flow's Reviewer time to post")
                return None
            goal = {"door": "B", "mode": "review", "merge_request_iid": iid,
                    "why": "The Merge Desk found no Reviewer verdict on this merge request."}
            if self.kick(mr, "review", "ship", goal, fp):
                self.notify.status(MR, iid, "Review", "running", now="The Reviewer is reviewing this merge request.")
            return None
        self.notify.status(MR, iid, "Review", "done", note=found["review"].get("verdict", "?"))
        insp = found.get("inspector")
        if insp and (insp.get("sha") == mr["sha"] or insp.get("fingerprint") == fp):
            self.notify.status(MR, iid, "Inspector", "done" if insp.get("verdict") == "matches" else "failed",
                               note=insp.get("verdict", "?"))
            return found
        checker, findings, jobs = context.pipeline_facts(self.gl, mr)
        bundle = evidence.build(self.gl, mr, checker, jobs, findings)
        if self.kick(mr, "inspector", "inspect", evidence.as_goal(bundle), fp):
            self.notify.status(MR, iid, "Inspector", "running",
                               now="The Inspector is checking the work against the request, from code-built evidence only.")
        return None

    def send_medic(self, mr: dict):
        """A Vallum MR whose pipeline failed: the Pipeline Medic gets at most 2 attempts (B19)."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        if tl.get("medic_sha") == mr["sha"]:
            return  # already handled for this commit
        attempts = int(tl.get("medic_attempts", 0))
        cap = int(self.pol["releases"]["max_fix_attempts"])
        hp = mr.get("head_pipeline") or {}
        if attempts >= cap or not self.pol.get("gate", {}).get("agent_verdicts_required"):
            self.store.set_timeline(target, medic_sha=mr["sha"], blocked_sha=mr["sha"])
            self.gl.mr_labels(mr["iid"], add=[self.pol.label("blocked"), self.pol.label("needs_human")],
                              remove=[self.pol.label("ready")])
            self.notify.status(MR, mr["iid"], "Checks", "failed", note=mr["sha"][:8], title=mr["title"],
                               now=f"The MR pipeline failed and the Pipeline Medic already had {attempts} attempt(s).",
                               action="look at the failing job; Vallum stopped retrying (cap reached).")
            return
        goal = {"merge_request_iid": mr["iid"], "source_branch": mr.get("source_branch"), "sha": mr["sha"],
                "pipeline_id": hp.get("id"), "attempt": attempts + 1, "max_attempts": cap}
        try:
            started = self.flows.start("medic", goal)
        except Exception as exc:
            self.log(f"!{mr['iid']}: could not start the Medic: {exc}")
            return
        self.store.set_timeline(target, medic_sha=mr["sha"], medic_attempts=attempts + 1)
        self.store.event("agent", f"!{mr['iid']}: Pipeline Medic attempt {attempts + 1}", mr_iid=mr["iid"])
        self.gl.mr_labels(mr["iid"], add=[self.pol.label("blocked")], remove=[self.pol.label("ready")])
        self.notify.status(MR, mr["iid"], "Pipeline Medic", "running", note=f"attempt {attempts + 1} of {cap}",
                           title=mr["title"], now=f"The pipeline failed on {mr['sha'][:8]}; the Medic is reading the job logs.",
                           action="nothing.")
        self.log(f"!{mr['iid']}: Pipeline Medic attempt {attempts + 1} (session {started.get('id')})")

    TRANSIENT = {6, 11, 12}   # stale rebase or pipeline, mode, queue: can change without a new commit

    def full_check(self, mr: dict, found: dict | None = None) -> dict:
        ctx = context.build(self.gl, mr, "desk")
        found = found or {}
        review, insp = found.get("review") or {}, found.get("inspector") or {}
        ctx.reviewer_verdict, ctx.reviewer_author = review.get("verdict"), review.get("author")
        ctx.inspector = insp or None
        ctx.fingerprint = evidence.fingerprint(self.gl.mr_changes(mr["iid"]))
        ctx.learned_writer = self.flows.bots().get(mr["author"]["username"]) == "incident"
        ctx.modes = self.store.doc("modes")
        ctx.safe_mode = bool(self.store.doc("meta").get("safe_mode"))
        ctx.queue_head = True
        ctx.caps_ok, ctx.caps_detail = True, "next in the queue; no caps hit"
        report = rules.evaluate(ctx)
        print(rules.render(report))
        return report

    @staticmethod
    def _not_passed(report: dict) -> set:
        return {r["no"] for r in report["rules"] if r["status"] not in ("pass", "waived")}

    def only_transient(self, report: dict) -> bool:
        return self._not_passed(report) <= self.TRANSIENT

    def only_stale(self, report: dict) -> bool:
        stale = [r for r in report["rules"] if r["no"] == 6 and "not rebased" in r["detail"]]
        return self._not_passed(report) == {6} and bool(stale)

    # ------------------------------------------------------------------ main
    def run(self) -> int:
        meta = self.store.doc("meta")
        if meta.get("safe_mode"):
            self.log(f"Safe mode is on ({meta.get('safe_mode_reason')}); not merging")
            self.notify.ledger()
            return 0
        release = self.store.release()
        if release.get("slot", "idle") != "idle":
            self.log(f"release {release.get('release_id')} is {release['slot']}; one release at a time")
            self.notify.ledger()
            return 0

        ready, waiting, failed = self.classify(self.gl.open_mrs(self.pol.label("opt_in")))
        # an MR that failed the full check stays out of the queue until it gets a new commit
        refused = [m for m in ready if self.store.timeline(f"{MR}-{m['iid']}").get("refused_sha") == m["sha"]]
        ready = [m for m in ready if m not in refused]
        ready.sort(key=lambda m: priority.queue_key(self.level(m), m.get("created_at")))
        self.save_queue(ready, waiting, failed + refused)
        for mr in failed:
            self.send_medic(mr)
        for pos, mr in enumerate(ready[1:], 2):
            tl = self.store.timeline(f"{MR}-{mr['iid']}")
            if tl.get("queue_pos") != pos:
                self.store.set_timeline(f"{MR}-{mr['iid']}", queue_pos=pos)
                self.notify.status(MR, mr["iid"], "Queued", "waiting", note=f"position {pos}", title=mr["title"],
                                   now=f"In the release queue at position {pos}. One release at a time.")
        if not ready:
            self.log(f"nothing ready ({len(waiting)} waiting, {len(failed)} failed, {len(refused)} refused)")
            self.notify.ledger()
            return 0

        head = ready[0]
        iid = head["iid"]
        self.log(f"next: !{iid} {head['title']}")
        self.gl.mr_labels(iid, add=[self.pol.label("ready")], remove=[self.pol.label("blocked")])
        self.notify.status(MR, iid, "Queued", "done", note="next", title=head["title"], priority=self.level(head),
                           issue_iid=evidence.related_issue(head.get("description")), now="Picked up by the Merge Desk.")
        found = self.agent_verdicts(head)
        if found is None:
            self.notify.ledger()
            return 0
        mr = self.rebase_and_wait(head)
        if not mr:
            self.notify.ledger()
            return 0

        report = self.full_check(mr, found)
        if not report["ok"] and self.only_stale(report):
            self.log(f"!{iid}: main moved during the check; rebasing once more")
            mr = self.rebase_and_wait(self.gl.mr(iid))
            if not mr:
                self.notify.ledger()
                return 0
            report = self.full_check(mr, found)
        tier = report["tier"]
        if not report["ok"]:
            failed = "; ".join(f"rule {r['no']}: {r['detail']}" for r in report["rules"] if r["status"] not in ("pass", "waived"))
            if self.only_transient(report):
                # main moved, a mode or the queue: nothing wrong with the MR itself, so retry next run
                self.notify.status(MR, iid, f"Gatekeeper {report['score']}", "waiting", tier=tier,
                                   now=f"Not merged yet: {failed}. The desk tries again on its next run.", action="nothing.")
            else:
                self.store.set_timeline(f"{MR}-{iid}", refused_sha=mr["sha"], blocked_sha=mr["sha"])
                self.notify.status(MR, iid, f"Gatekeeper {report['score']}", "failed", tier=tier,
                                   now=f"Not merged. {failed}", action="fix the failed rules and push; the desk retries after a new commit.")
                self.gl.mr_labels(iid, add=[self.pol.label("blocked")], remove=[self.pol.label("ready")])
            self.notify.ledger()
            return 0

        release_id = self.store.next_release_id()
        feature = ", ".join(report["features"]) or "no feature change"
        deadline = utcnow() + timedelta(minutes=float(self.pol["watchdog"]["deadlines_min"]["merged"]))
        try:
            self.store.claim_release(("idle",), slot="merged", release_id=release_id, mr_iid=iid, sha=mr["sha"],
                                     feature=feature, features=report["features"], touched=report["touched"], tier=tier, author=mr["author"]["username"], title=mr["title"],
                                     merged_at=None, deadline=iso(deadline), restarted=False, abort=False)
        except Abort as exc:
            self.log(f"could not claim the release slot: {exc}")
            return 0
        self.store.sanction(mr["sha"], mr_iid=iid, release_id=release_id)
        self.store.save_release(release_id, {
            "release": release_id, "mr": iid, "title": mr["title"], "requester": mr["author"]["username"],
            "feature": feature, "tier": tier, "started_at": iso(),
            "decisions": [
                {"by": "membership-check", "result": next(r["detail"] for r in report["rules"] if r["no"] == 1)},
                {"by": "gatekeeper", "rules": report["score"], "sha": mr["sha"], "at": iso()},
            ],
            "human_actions": 0,
        })
        self.notify.status(MR, iid, f"Gatekeeper {report['score']}", "done", tier=tier, release_id=release_id,
                           now="All rules pass. Approving and merging.")
        try:
            self.approve(iid, mr["sha"])
            merged = self.merge(iid, mr["sha"])
        except Exception as exc:
            self.log(f"merge failed: {exc}")
            self.store.unsanction(mr["sha"])
            self.store.set_release(slot="idle", release_id=None)
            self.notify.status(MR, iid, "Merged", "failed", now=f"GitLab refused the merge: {str(exc)[:200]}",
                               action="check the MR approval rule allows the owner to approve (see README).")
            self.notify.ledger()
            return 1
        merge_sha = merged.get("merge_commit_sha") or merged.get("sha")
        self.store.set_release(merged_at=iso(), merge_sha=merge_sha)
        self.store.event("merged", f"{release_id} !{iid} {mr['title']} merged ({report['score']})",
                         release_id=release_id, mr_iid=iid)
        self.notify.status(MR, iid, "Merged", "done", note=(merge_sha or "")[:8],
                           now="Merged. The main pipeline builds the image; the test drive starts next.")
        self.notify.ledger()
        self.log(f"merged !{iid} as {merge_sha} ({release_id})")
        return 0


def main():
    pol = config.policy()
    gl = GitLab.from_env(require_token=True)
    store = Store.from_env()
    sys.exit(Desk(gl, store, pol).run())


if __name__ == "__main__":
    main()
