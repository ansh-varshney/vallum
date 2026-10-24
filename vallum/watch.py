"""Quick reactions while the Live Patrol is listening (B22, B23).

GitLab has no trigger for label changes, finished MR pipelines or new commits, and the Merge Desk normally waits
for the next schedule. While listening, the patrol fingerprints every open Vallum merge request each tick. When
one changes (a person removed `vallum::needs-human`, pushed a commit, a pipeline finished), it

  * acknowledges a released hold on the status card at once, and
  * re-runs the `merge-desk` job of its own pipeline (a normal job retry with the owner's account), so the desk
    acts within about a minute instead of at the next schedule. The job keeps `resource_group: release`, so it
    still queues behind a running release. At most one kick every `cockpit.desk_kick_min_s` seconds.
"""

import os
import time

from vallum.config import iso, utcnow

MR = "merge_requests"
DOC = "watch"
RUNNING = {"created", "pending", "running", "waiting_for_resource", "preparing", "scheduled"}


def fingerprint(mr: dict, needs_human: str, pipeline: str | None = None) -> list:
    return [mr.get("sha"), mr.get("detailed_merge_status"), needs_human in (mr.get("labels") or []),
            sorted(r.get("id") for r in mr.get("reviewers") or []), pipeline]


class Watcher:
    def __init__(self, gl, store, pol, notify, log=print, pipeline_id: str | None = None, clock=time.time):
        self.gl, self.store, self.pol, self.notify, self.log = gl, store, pol, notify, log
        self.pipeline_id = pipeline_id or os.environ.get("CI_PIPELINE_ID")
        self.clock = clock

    def tick(self) -> bool:
        """One look at the open Vallum MRs. Returns True if anything changed since the last look."""
        needs_human = self.pol.label("needs_human")
        mrs = self.gl.open_mrs(self.pol.label("opt_in")) or []
        before = self.store.doc(DOC).get("mrs")
        now = {str(m["iid"]): fingerprint(m, needs_human, self.pipeline_status(m["iid"])) for m in mrs}
        if before is None:                      # first look ever: remember, don't react
            self.store.update(DOC, lambda cur: {**cur, "mrs": now})
            return False
        changed = [iid for iid, fp in now.items() if before.get(iid) != fp]
        if not changed:
            return False
        self.store.update(DOC, lambda cur: {**cur, "mrs": now})
        for iid in changed:
            old = before.get(iid)
            if old and old[2] and not now[iid][2]:
                self.hold_released(int(iid))
        self.log(f"watch: change on {', '.join('!' + i for i in changed)}")
        self.kick_desk()
        return True

    def pipeline_status(self, iid: int) -> str | None:
        """The MR's latest pipeline (a failure or a pass must wake the desk even if the merge state is unchanged)."""
        try:
            pipes = self.gl.get(f"/projects/:id/merge_requests/{iid}/pipelines", per_page=1) or []
        except Exception:
            return None
        return f"{pipes[0].get('id')}:{pipes[0].get('status')}" if pipes else None

    def hold_released(self, iid: int):
        tl = self.store.timeline(f"{MR}-{iid}")
        if not tl.get("held_for"):
            return
        self.notify.status(MR, iid, "Needs a human", "done", note="released",
                           now=f"✅ You released the hold at {utcnow().strftime('%H:%M')} UTC. "
                               "The Merge Desk is picking this up now and will update this card.",
                           action="nothing.")

    def kick_desk(self) -> str:
        last = float(self.store.doc(DOC).get("last_kick_ts") or 0)
        gap = float(self.pol.get("cockpit", {}).get("desk_kick_min_s", 120))
        if self.clock() - last < gap:
            return "too soon"
        if not self.pipeline_id:
            return "no pipeline"
        jobs = self.gl.get(f"/projects/:id/pipelines/{self.pipeline_id}/jobs", per_page=100) or []
        desk = max((j for j in jobs if j.get("name") == "merge-desk"), key=lambda j: j["id"], default=None)
        if not desk:
            return "no desk job"
        if desk.get("status") in RUNNING:
            return "desk already running"
        self.store.update(DOC, lambda cur: {**cur, "last_kick_ts": self.clock(), "last_kick": iso()})
        new = self.gl.post(f"/projects/:id/jobs/{desk['id']}/retry")
        self.log(f"watch: re-ran the Merge Desk now (job {new.get('id')})")
        return "kicked"
