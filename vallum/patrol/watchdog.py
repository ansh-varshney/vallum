"""The Watchdog (B18): zero-credit code on a schedule. If the work on a release or a work item goes
silent past its deadline, it applies that state's known undo. Production is never left half-changed.

    state                         if the deadline passes...
    planning/building/reviewing   MR to draft, label stalled, explain, free the lease   (leases, Layer 2)
    gated                         free the release slot and re-queue
    merged, not driving           start the main pipeline again with the owner token, once only
    driving                       send 100% of traffic back to the last good revision, open an incident
"""

from vallum.config import iso, parse_iso, utcnow


def decide(release: dict, now=None, drive_ended: bool = False) -> str | None:
    """Pure decision so it can be unit-tested: which undo applies to the release slot, if any. A test drive whose
    job already ended while the slot still says 'driving' (it crashed) is recovered at once, not at the deadline."""
    now = now or utcnow()
    slot = release.get("slot", "idle")
    if slot == "driving" and drive_ended:
        return "rollback"
    deadline = parse_iso(release.get("deadline"))
    if slot == "idle" or not deadline or now <= deadline:
        return None
    if slot == "gated":
        return "free_slot"
    if slot == "merged":
        return "free_slot" if release.get("restarted") else "restart_pipeline"
    if slot == "driving":
        return "rollback"
    return None


def decide_lease(lease: dict, now=None) -> str | None:
    now = now or utcnow()
    deadline = parse_iso(lease.get("deadline"))
    if lease.get("state") in ("planning", "building", "reviewing") and deadline and now > deadline:
        return "stall"
    return None


class Watchdog:
    def __init__(self, gl, store, pol, notifier, cloudrun_factory):
        self.gl = gl
        self.store = store
        self.pol = pol
        self.notify = notifier
        self.cloudrun_factory = cloudrun_factory

    def run(self) -> list[str]:
        actions = []
        release = self.store.release()
        ended = self.drive_ended(release)
        if ended:
            release = {**release, "drive_job": ended}
        action = decide(release, drive_ended=bool(ended))
        if action:
            actions.append(getattr(self, action)(release))
        for key, lease in self.store.b.list("vallum_leases"):
            if decide_lease(lease) == "stall":
                actions.append(self.stall(key, lease))
        return actions

    def drive_ended(self, release: dict) -> str | None:
        """The status of the test-drive job holding the slot, if that job has already ended."""
        pipeline = release.get("drive_pipeline")
        if release.get("slot") != "driving" or not pipeline:
            return None
        try:
            jobs = self.gl.get(f"/projects/:id/pipelines/{pipeline}/jobs", per_page=100) or []
        except Exception:
            return None
        job = next((j for j in jobs if j.get("name") == "test-drive"), None)
        if not job or job.get("status") not in ("success", "failed", "canceled"):
            return None
        # a drive that finished normally freed the slot just before its job ended: read the slot again
        now = self.store.release()
        if now.get("slot") != "driving" or now.get("release_id") != release.get("release_id"):
            return None
        return job["status"]

    def free_slot(self, release: dict) -> str:
        rid = release.get("release_id")
        self.store.set_release(slot="idle", candidate=None, step=None, abort=False)
        self.store.event("watchdog", f"freed the release slot held by {rid} ({release.get('slot')} past its deadline)")
        self.notify.alert("merge_requests", release.get("mr_iid"), "Watchdog freed the release slot", author=release.get("author"),
                          what=f"Release {rid} stayed '{release.get('slot')}' past its deadline.",
                          impact="None in production; the next release was waiting.",
                          done="Freed the release slot so the queue can move.",
                          nxt="If the change was merged, the next main pipeline will deploy it with the next release.",
                          action="No.")
        return f"free_slot {rid}"

    def restart_pipeline(self, release: dict) -> str:
        from datetime import timedelta

        rid = release.get("release_id")
        self.gl.post("/projects/:id/pipeline", json={"ref": "main"})
        minutes = float(self.pol["watchdog"]["deadlines_min"]["merged"])
        self.store.set_release(restarted=True, deadline=iso(utcnow() + timedelta(minutes=minutes)))
        self.store.event("watchdog", f"{rid} merged but not driving: restarted the main pipeline once")
        return f"restart_pipeline {rid}"

    def rollback(self, release: dict) -> str:
        rid = release.get("release_id")
        old = release.get("old_revision") or release.get("last_good")
        cr = self.cloudrun_factory()
        if old:
            cr.set_traffic([{"revisionName": old, "percent": 100}])
        self.store.set_release(slot="idle", candidate=None, step=None, abort=False, last_good=old)
        self.store.record_rollback(float(self.pol["circuit_breaker"]["within_hours"]), int(self.pol["circuit_breaker"]["rollbacks"]))
        job = release.get("drive_job")
        how = (f"its test-drive job ended ({job}) without finishing the release" if job
               else f"its test drive stopped reporting before its deadline ({release.get('deadline')})")
        incident = self.gl.create_issue(
            f"Incident: {rid} test drive died mid-way; Watchdog rolled back",
            f"🛡 Vallum: release `{rid}` was left half-done: {how} "
            f"(pipeline {release.get('drive_pipeline')}). The Watchdog sent 100% of traffic back to `{old}`.",
            [self.pol.label("incident")])
        self.store.event("rolled_back", f"Watchdog rolled back {rid}: the drive died", incident=incident["iid"])
        self.notify.alert("merge_requests", release.get("mr_iid"), "Rolled back by the Watchdog", author=release.get("author"),
                          what=f"Release {rid} was left half-done: {how}.",
                          impact=f"Visitors on the new version were moved back to `{old}`.",
                          done=f"Sent 100% of traffic to `{old}`; opened incident #{incident['iid']}.",
                          nxt="The change stays merged but not live.", action="No.")
        return f"rollback {rid}"

    def stall(self, key: str, lease: dict) -> str:
        mr_iid, issue_iid = lease.get("mr_iid"), lease.get("issue_iid")
        if mr_iid:
            mr = self.gl.mr(int(mr_iid))
            if not mr.get("draft") and not mr["title"].startswith("Draft:"):
                self.gl.put(f"/projects/:id/merge_requests/{mr_iid}", json={"title": "Draft: " + mr["title"]})
            self.gl.mr_labels(int(mr_iid), add=[self.pol.label("stalled")], remove=[self.pol.label("ready")])
        target_kind, target = ("issues", issue_iid) if issue_iid else ("merge_requests", mr_iid)
        self.notify.alert(target_kind, target, "Work stalled", author=lease.get("requester"),
                          what=f"The agent session working on this ({lease.get('state')}) stopped before its deadline.",
                          impact="None: nothing is live.", done="Set the MR to draft and labelled it stalled.",
                          nxt="The Live Patrol restarts it if the flow-run cap allows.", action="No.")
        self.store.b.delete("vallum_leases", key)
        return f"stall {key}"
