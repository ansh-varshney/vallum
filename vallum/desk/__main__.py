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

from vallum import config, priority
from vallum.config import iso, utcnow
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

        ready, waiting, blocked = self.classify(self.gl.open_mrs(self.pol.label("opt_in")))
        # an MR that failed the full check stays out of the queue until it gets a new commit
        refused = [m for m in ready if self.store.timeline(f"{MR}-{m['iid']}").get("gate_failed_sha") == m["sha"]]
        ready = [m for m in ready if m not in refused]
        blocked += refused
        ready.sort(key=lambda m: priority.queue_key(self.level(m), m.get("created_at")))
        self.save_queue(ready, waiting, blocked)
        for mr in blocked:
            tl = self.store.timeline(f"{MR}-{mr['iid']}")
            if tl.get("blocked_sha") != mr["sha"]:
                self.store.set_timeline(f"{MR}-{mr['iid']}", blocked_sha=mr["sha"])
                self.notify.status(MR, mr["iid"], "Checks", "failed", note=mr["sha"][:8], title=mr["title"],
                                   now="The MR pipeline failed, so the Merge Desk won't pick this up yet.",
                                   action="fix the failing job (Layer 2: the Pipeline Medic does this).")
                self.gl.mr_labels(mr["iid"], add=[self.pol.label("blocked")], remove=[self.pol.label("ready")])
        for pos, mr in enumerate(ready[1:], 2):
            tl = self.store.timeline(f"{MR}-{mr['iid']}")
            if tl.get("queue_pos") != pos:
                self.store.set_timeline(f"{MR}-{mr['iid']}", queue_pos=pos)
                self.notify.status(MR, mr["iid"], "Queued", "waiting", note=f"position {pos}", title=mr["title"],
                                   now=f"In the release queue at position {pos}. One release at a time.")
        if not ready:
            self.log(f"nothing ready ({len(waiting)} waiting, {len(blocked)} blocked)")
            self.notify.ledger()
            return 0

        head = ready[0]
        iid = head["iid"]
        self.log(f"next: !{iid} {head['title']}")
        self.gl.mr_labels(iid, add=[self.pol.label("ready")], remove=[self.pol.label("blocked")])
        self.notify.status(MR, iid, "Queued", "done", note="next", title=head["title"], priority=self.level(head),
                           now="Picked up by the Merge Desk.")
        mr = self.rebase_and_wait(head)
        if not mr:
            self.notify.ledger()
            return 0

        ctx = context.build(self.gl, mr, "desk")
        ctx.modes = self.store.doc("modes")
        ctx.safe_mode = bool(self.store.doc("meta").get("safe_mode"))
        ctx.queue_head = True
        ctx.caps_ok, ctx.caps_detail = True, "next in the queue; no caps hit"
        report = rules.evaluate(ctx)
        print(rules.render(report))
        tier = report["tier"]
        if not report["ok"]:
            failed = "; ".join(f"rule {r['no']}: {r['detail']}" for r in report["rules"] if r["status"] != "pass" and r["status"] != "waived")
            self.store.set_timeline(f"{MR}-{iid}", gate_failed_sha=mr["sha"], blocked_sha=mr["sha"])
            self.notify.status(MR, iid, f"Gatekeeper {report['score']}", "failed", tier=tier,
                               now=f"Not merged. {failed}", action="fix the failed rules; the desk retries on its next run.")
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
