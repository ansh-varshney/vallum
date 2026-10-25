"""The Merge Desk (B17): the only thing that merges. Runs on main with resource_group: release,
on a schedule and at the end of every main pipeline. One release at a time.

For the next ready MR it: checks the freeze, makes sure the Reviewer and the Inspector have spoken on this
exact change (starting them if not), rebases onto main, waits for a fresh pipeline on that commit, runs all 12
Gatekeeper rules, records the commit as sanctioned for the provenance gate, gives the owner's approval (the MR
approval rule needs it) and merges pinned to that SHA.

Agents are started the way a member would start them (assigning the flow's bot as reviewer), so they run on
the flows' service accounts; only the Inspector, whose input is the evidence bundle, uses the Flows API.

    python -m vallum.desk
"""

import os
import sys
import time
from datetime import timedelta

from vallum import cards as cardlib
from vallum import agents, config, evidence, priority, verdicts
from vallum.config import iso, parse_iso, utcnow
from vallum.flows import DEAD, DONE, Flows, final_json
from vallum.gate import context, rules
from vallum.gitlab import GitLab, GitLabError
from vallum.notify import Notifier
from vallum.state import Abort, Store

MR = "merge_requests"
FINISHED = {"success", "failed", "canceled", "skipped"}
FIX_LEVELS = ("P0", "P1")


class Desk:
    def __init__(self, gl: GitLab, store: Store, pol):
        self.gl = gl
        self.store = store
        self.pol = pol
        self.notify = Notifier(gl, store, pol)
        self.flows = Flows(gl, store)
        self._main_cards = None

    def log(self, msg: str):
        print(f"[merge-desk {utcnow().strftime('%H:%M:%S')}] {msg}", flush=True)

    @property
    def main_cards(self) -> cardlib.CardSet:
        if self._main_cards is None:
            self._main_cards = cardlib.load_from_gitlab(self.gl, os.environ.get("CI_DEFAULT_BRANCH", "main"))
        return self._main_cards

    # ------------------------------------------------------------------ queue
    def classify(self, mrs: list[dict]):
        ready, waiting, failed = [], [], []
        for mr in mrs:
            if mr.get("draft") or mr.get("work_in_progress"):
                continue
            full = self.gl.mr(mr["iid"])
            hp = full.get("head_pipeline") or {}
            if hp.get("sha") == full["sha"] and hp.get("status") == "success":
                ready.append(full)
            elif hp.get("sha") == full["sha"] and hp.get("status") in FINISHED:
                failed.append(full)
            else:
                waiting.append(full)
        return ready, waiting, failed

    def level(self, mr: dict) -> str:
        level = priority.label_override(mr.get("labels")) or "P2"
        return priority.aged(level, mr.get("created_at"))

    def save_queue(self, ready, waiting, failed, paused, held, refused=()):
        items = [f"!{m['iid']} {m['title']} · {self.level(m)} · ready" for m in ready]
        items += [f"!{m['iid']} {m['title']} · {self.level(m)} · paused: {why}" for m, why in paused]
        items += [f"!{m['iid']} {m['title']} · waiting for a human decision" for m in held]
        items += [f"!{m['iid']} {m['title']} · waiting for its pipeline" for m in waiting]
        items += [f"!{m['iid']} {m['title']} · pipeline failed" for m in failed]
        items += [f"!{m['iid']} {m['title']} · refused by the Gatekeeper (waits for a new commit)" for m in refused]
        self.store.update("queue", lambda _cur: {"items": items, "at": iso()})

    def close_steps(self, iid: int, steps: list[str]):
        """Mark running or failed steps done once the MR has moved past them (status card stays truthful)."""
        tl = self.store.timeline(f"{MR}-{iid}")
        for m in tl.get("milestones", []):
            if m["step"] in steps and m["state"] in ("running", "failed", "waiting"):
                self.notify.status(MR, iid, m["step"], "done", note=m.get("note", ""))

    # ------------------------------------------------------------------ freeze (B10, FR-BUDGET-3)
    def freeze_reason(self, mr: dict) -> str | None:
        modes = self.store.doc("modes")
        tl = self.store.timeline(f"{MR}-{mr['iid']}")
        if tl.get("override") or self.pol.label("override") in mr.get("labels", []):
            return None
        files, _lines, _added = context.mr_changes(self.gl, mr["iid"])
        touched = cardlib.blast_radius(files, self.main_cards.cards)
        tiers = {self.main_cards.cards[n].get("tier") for n in touched if n in self.main_cards.cards}
        is_fix = self.pol.label("fix") in mr.get("labels", [])
        for path in files:
            if path.startswith("promises/features/") and path.endswith(".yaml"):
                text = self.gl.file_raw_or_none(path, mr["sha"])
                card = cardlib.parse_card_text(text) if text else {}
                if card.get("tier"):
                    tiers.add(card["tier"])
                is_fix = is_fix or card.get("kind") in ("bug-fix", "security-fix")
        if is_fix and self.level(mr) in FIX_LEVELS:
            return None
        if (modes.get("app") or {}).get("mode") == "red":
            return "the app-wide allowance is RED"
        red = sorted(t for t in tiers if (modes.get(t) or {}).get("mode") == "red")
        return f"{', '.join(red)} is RED" if red else None

    def pause(self, mr: dict, why: str):
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        if tl.get("paused_why") == why:
            return
        self.store.set_timeline(target, paused_why=why, paused_at=iso())
        self.gl.mr_labels(mr["iid"], add=[self.pol.label("paused")], remove=[self.pol.label("ready")])
        self.notify.status(MR, mr["iid"], "Paused", "paused", note=why, title=mr["title"],
                           issue_iid=evidence.related_issue(mr.get("description")),
                           now=f"Paused because {why}. It stays queued and resumes by itself when the tier recovers.",
                           action="nothing (or `@vallum ship-anyway !" + str(mr["iid"]) + "` to override).")
        self.notify.alert(MR, mr["iid"], "Paused", author=mr["author"]["username"],
                          what=f"{mr['title']} is paused: {why}.",
                          impact="Nothing changed in production; this change is waiting in the queue.",
                          done="Kept it queued; other changes that don't touch that tier still ship.",
                          nxt="It resumes automatically when the tier's allowance recovers, and you'll be told.",
                          action="No. A member can override with `@vallum ship-anyway !" + str(mr["iid"]) + "`.")
        self.store.event("paused", f"!{mr['iid']} paused: {why}", mr_iid=mr["iid"])

    def unpause(self, mr: dict):
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        if not tl.get("paused_why"):
            return
        self.store.set_timeline(target, paused_why=None, resumed_at=iso())
        self.gl.mr_labels(mr["iid"], remove=[self.pol.label("paused")])
        self.notify.status(MR, mr["iid"], "Paused", "done", note="resumed", now="Resumed: the tier recovered.")
        self.notify.alert(MR, mr["iid"], "Resumed", author=mr["author"]["username"],
                          what=f"{mr['title']} is no longer paused: the tier recovered.",
                          impact="None yet.", done="Back in the release queue.",
                          nxt="The Merge Desk takes it in priority order.", action="No.")
        self.store.event("resumed", f"!{mr['iid']} resumed", mr_iid=mr["iid"])

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
                                   now="The rebase hit a conflict; the Pipeline Medic gets the merge request.",
                                   action="nothing yet.")
                self.send_medic(mr, reason="rebase conflict")
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

    # ------------------------------------------------------------------ agents
    def usage_ok(self) -> bool:
        return agents.usage_ok(self.store, self.pol, self.notify)

    def count_usage(self, key: str):
        agents.count_usage(self.store, key)

    def session_running(self, mr_iid: int, sess: dict, key: str) -> bool:
        begun = parse_iso(sess.get("at"))
        wait_min = float(self.pol["watchdog"]["deadlines_min"].get("reviewing", 15))
        if not begun or utcnow() - begun > timedelta(minutes=wait_min * 2):
            return False
        sid = sess.get("id")
        if not sid and sess.get("via") == "trigger":
            found = self.flows.sessions_on(MR, mr_iid, key, sess["at"])
            sid = found[-1]["id"] if found else None
            if not sid:
                return utcnow() - begun < timedelta(minutes=10)   # the bot hasn't reported its session yet
        status = self.flows.session_status(sid) if sid else None
        return status not in DONE and status not in DEAD

    def start_agent(self, mr: dict, kind: str, key: str, fp: str, goal=None) -> bool:
        """Start one agent session on this MR unless one is already running (FR-GATE-5) or a cap is hit."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        for other_kind, other in (tl.get("sessions") or {}).items():
            other_key = {"inspector": "inspect", "medic": "medic"}.get(other_kind, "ship")
            if self.session_running(mr["iid"], other, other_key):
                self.log(f"!{mr['iid']}: {other_kind} session is still running; waiting")
                return False
        sess = (tl.get("sessions") or {}).get(kind) or {}
        started = parse_iso(sess.get("at"))
        wait_min = float(self.pol["watchdog"]["deadlines_min"].get("reviewing", 15))
        if sess.get("fp") == fp and started and utcnow() - started < timedelta(minutes=wait_min):
            self.log(f"!{mr['iid']}: {kind} already ran on this exact change")
            return False
        cap = int(self.pol["releases"]["max_flow_runs_per_issue"])
        if int(tl.get("flow_runs", 0)) >= cap and kind != "inspector":
            self.hold_for_human(mr, f"Vallum ran {cap} agent sessions on this merge request without a verdict.",
                                ["Look at the sessions and fix or answer the open points yourself",
                                 "Remove the label vallum::needs-human to give Vallum another try"])
            return False
        if not self.usage_ok():
            return False
        via, sid = "api", None
        try:
            if key != "inspect" and self.pol.get("agents", {}).get("start_via_triggers", True) \
                    and self.flows.start_by_trigger(key, MR, mr["iid"], as_reviewer=True):
                via = "trigger"
            else:
                sid = self.flows.start(key, goal if goal is not None else {"merge_request_iid": mr["iid"], "mode": kind}).get("id")
        except Exception as exc:
            self.log(f"!{mr['iid']}: could not start {kind}: {exc}")
            return False
        sessions = dict(tl.get("sessions") or {})
        sessions[kind] = {"id": sid, "via": via, "fp": fp, "at": iso(), "sha": mr["sha"]}
        runs = int(tl.get("flow_runs", 0)) + (0 if kind == "inspector" else 1)
        self.store.set_timeline(target, sessions=sessions, flow_runs=runs)
        self.count_usage(key)
        self.store.event("agent", f"!{mr['iid']}: started {kind} ({via})", mr_iid=mr["iid"])
        self.log(f"!{mr['iid']}: started {kind} via {via}" + (f" (session {sid})" if sid else ""))
        return True

    def hold_for_human(self, mr: dict, question: str, options: list[str], note_id=None):
        """FR-LOOP-4: stop agent work on this MR, label it and ask the owner, once."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        if tl.get("held_for") == (note_id or question):
            return
        self.store.set_timeline(target, held_for=note_id or question, held_at=iso())
        self.gl.mr_labels(mr["iid"], add=[self.pol.label("needs_human")], remove=[self.pol.label("ready")])
        opts = "\n".join(f"  {i}. {o}" for i, o in enumerate(options, 1)) if options else ""
        self.notify.status(MR, mr["iid"], "Needs a human", "waiting", title=mr["title"],
                           issue_iid=evidence.related_issue(mr.get("description")),
                           now=f"Stopped agent work: {question}",
                           action="decide, say so in a comment, then remove the label vallum::needs-human.")
        self.notify.alert(MR, mr["iid"], "Needs a human", author=self.pol.owners[0] if self.pol.owners else None,
                          what=question + (f"\n{opts}" if opts else ""),
                          impact="Nothing changed in production. This merge request is on hold.",
                          done="Stopped all agent work on it and added the label vallum::needs-human.",
                          nxt="When you remove the label, Vallum reads your latest comment and continues.",
                          action="Yes: decide, comment, and remove the label.")
        self.store.event("needs-human", f"!{mr['iid']}: {question[:120]}", mr_iid=mr["iid"])

    def human_released(self, mr: dict) -> bool:
        """The owner removed the needs-human label after Vallum held the MR: continue, with a fresh session budget."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        if tl.get("held_for") and self.pol.label("needs_human") not in mr.get("labels", []):
            self.store.set_timeline(target, held_for=None, released_at=iso(), released_for=tl.get("held_for"),
                                    flow_runs=0)
            self.close_steps(mr["iid"], ["Needs a human"])
            self.notify.status(MR, mr["iid"], "Needs a human", "done", note="released",
                               now="You released the hold. Vallum is continuing.", action="nothing.")
            self.store.event("needs-human", f"!{mr['iid']}: released by a human", mr_iid=mr["iid"])
            return True
        return False

    def agent_verdicts(self, mr: dict) -> dict | None:
        """Make sure the Reviewer and Inspector have spoken on this change; start them if not.
        Returns the verdicts when both exist, None while waiting (FR-INSP-4: a new change needs a new inspection)."""
        if not self.pol.get("gate", {}).get("agent_verdicts_required") or context.is_housekeeping(self.gl, mr, self.pol):
            return {}
        iid = mr["iid"]
        tl = self.store.timeline(f"{MR}-{iid}")
        released = self.human_released(mr)
        if tl.get("held_for") and not released:
            return None
        found = verdicts.latest(self.gl.notes(MR, iid), self.flows.bots())
        fp = evidence.fingerprint(self.gl.mr_changes(iid))
        built_by_vallum = bool(context.agent_authors(self.gl, iid))
        review = found.get("review")
        if review and review.get("verdict") == "needs-human" and not released \
                and tl.get("released_for") != review.get("note_id"):
            self.hold_for_human(mr, review.get("decision_needed") or "The Reviewer asked for a person's decision.",
                                review.get("options") or [], note_id=review.get("note_id"))
            return None
        reviewers = self.gl.get(f"/projects/:id/merge_requests/{iid}/reviewers") or []
        changes_requested = (mr.get("detailed_merge_status") == "requested_changes"
                             or any(r.get("state") == "requested_changes" for r in reviewers)
                             or (review or {}).get("verdict") == "changes"
                             or (released and (review or {}).get("verdict") == "needs-human"))
        if changes_requested:
            if built_by_vallum:
                goal = {"door": "A", "mode": "fix", "merge_request_iid": iid,
                        "why": "Open review points or an owner decision on a merge request Vallum built."}
                if self.start_agent(mr, "fix", "ship", fp, goal):
                    self.notify.status(MR, iid, "Review fixes", "running",
                                       now="Open review points: Vallum's Builder and Reviewer are addressing them.")
            else:
                self.notify.status(MR, iid, "Changes requested", "waiting",
                                   now="A reviewer requested changes; waiting for the author's next commit.",
                                   action="push a commit that answers the review points.")
            return None
        stale = bool(review) and review.get("verdict") != "approve" and review.get("sha")             and review.get("sha") != mr["sha"]
        if stale:
            # the change moved on since that verdict (a fix round, the Medic, a rebase): it needs a fresh review
            self.log(f"!{iid}: the Reviewer's '{review.get('verdict')}' was for {str(review.get('sha'))[:8]}; asking again")
            goal = {"door": "A" if built_by_vallum else "B", "mode": "review", "merge_request_iid": iid}
            if self.start_agent(mr, "review", "ship", fp, goal):
                self.notify.status(MR, iid, "Review", "running", now="The change moved on since the last verdict; the Reviewer is reviewing it again.")
            return None
        if not review:
            age = utcnow() - (parse_iso(mr.get("created_at")) or utcnow())
            if age < timedelta(minutes=8) and built_by_vallum:
                self.log(f"!{iid}: young MR; giving the Ship flow's Reviewer time to post")
                return None
            goal = {"door": "A" if built_by_vallum else "B", "mode": "review", "merge_request_iid": iid}
            if self.start_agent(mr, "review", "ship", fp, goal):
                self.notify.status(MR, iid, "Review", "running", now="The Reviewer is reviewing this merge request.")
            return None
        self.close_steps(iid, ["Review fixes", "Changes requested"])
        self.notify.status(MR, iid, "Review", "done", note=review.get("verdict", "?"))
        insp = self.inspector_verdict(mr, fp)
        if insp and (insp.get("sha") == mr["sha"] or insp.get("fingerprint") == fp):
            found["inspector"] = insp
            self.notify.status(MR, iid, "Inspector", "done" if insp.get("verdict") == "matches" else "failed",
                               note=insp.get("verdict", "?"))
            return found
        checker, findings, jobs = context.pipeline_facts(self.gl, mr)
        bundle = evidence.build(self.gl, mr, checker, jobs, findings)
        if self.start_agent(mr, "inspector", "inspect", fp, evidence.as_goal(bundle)):
            self.notify.status(MR, iid, "Inspector", "running",
                               now="The Inspector is checking the work against the request, from code-built evidence only.")
        return None

    def request_key(self, mr: dict) -> str:
        """What else the Inspector judged besides the change: the request text and its own instructions. A verdict
        is reused only while both are the same (seen live: a corrected request kept its old 'gaps')."""
        import hashlib

        text = mr.get("description") or ""
        issue = evidence.related_issue(text)
        if issue:
            try:
                text = self.gl.get(f"/projects/:id/issues/{issue}").get("description") or ""
            except Exception:
                pass
        definition = (self.flows.registry("inspect") or {}).get("definition_sha", "") if self.flows else ""
        return hashlib.sha256(f"{definition}\n{text}".encode()).hexdigest()[:16]

    def inspector_verdict(self, mr: dict, fp: str) -> dict | None:
        """The Inspector's verdict lives in the state store, read by code from its own session trace.
        Comments can't stand in for it (FR-INSP-1, FR-INSP-5)."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        stored = tl.get("inspector_verdict")
        if stored and stored.get("fingerprint") == fp and stored.get("request_key") == self.request_key(mr):
            return stored
        sess = (tl.get("sessions") or {}).get("inspector") or {}
        if not sess.get("id") or sess.get("fp") != fp:
            return None
        status = self.flows.session_status(sess["id"])
        if status in DEAD:
            sessions = dict(tl.get("sessions") or {})
            sessions.pop("inspector", None)
            self.store.set_timeline(target, sessions=sessions)
            self.log(f"!{mr['iid']}: Inspector session {sess['id']} ended as {status}; it will be restarted")
            return None
        if status not in DONE:
            return None
        answer = final_json(self.flows.trace(sess["id"]))
        if not answer or answer.get("verdict") not in ("matches", "gaps"):
            self.log(f"!{mr['iid']}: Inspector session {sess['id']} gave no readable verdict")
            sessions = dict(tl.get("sessions") or {})
            sessions.pop("inspector", None)
            self.store.set_timeline(target, sessions=sessions)
            return None
        # trust our own record of what was inspected, not the agent's copy of it
        if str(answer.get("fingerprint")) != fp:
            answer["gaps"] = list(answer.get("gaps") or []) + ["the verdict names a different change than the bundle"]
            answer["verdict"] = "gaps"
        verdict = {"verdict": answer["verdict"], "sha": sess.get("sha"), "fingerprint": fp, "session_id": sess["id"],
                   "trace": answer.get("trace") or [], "gaps": answer.get("gaps") or [],
                   "unexplained_changes": answer.get("unexplained_changes") or [],
                   "injected_instructions": answer.get("injected_instructions") or [], "at": iso(),
                   "author": "inspector-session", "request_key": self.request_key(mr)}
        self.store.set_timeline(target, inspector_verdict=verdict)
        rows = "\n".join(f"| {t.get('done_when', '')} | `{t.get('promise_id', '')}` | {t.get('result', '')} |"
                         for t in verdict["trace"][:20]) or "| (none) | | |"
        extra = ""
        for name in ("gaps", "unexplained_changes", "injected_instructions"):
            if verdict[name]:
                extra += f"\n**{name.replace('_', ' ').capitalize()}:** " + "; ".join(map(str, verdict[name][:5]))
        self.gl.add_note(MR, mr["iid"], (
            f"🛡 Vallum: **Inspector verdict** · {verdict['verdict']} (read by code from agent session {sess['id']})\n\n"
            "| Done when | Promise | Result |\n|---|---|---|\n" + rows + extra +
            f"\n\n_AI-written inspection from a code-built evidence bundle (no comments in it); "
            f"inspected change `{fp}`._"))
        self.store.event("agent", f"!{mr['iid']}: Inspector says {verdict['verdict']}", mr_iid=mr["iid"])
        return verdict

    def main_head(self) -> str | None:
        """A refusal holds until the MR gets a new commit or main moves (main may have fixed the cause)."""
        try:
            return self.gl.get(f"/projects/:id/repository/branches/{self.pol['project'].get('default_branch', 'main')}")["commit"]["id"]
        except Exception:
            return None

    def rebase_if_main_moved(self, mr: dict, tl: dict) -> bool:
        """A failed MR behind main: rebase it for free (no credits) once per main commit, since main may already
        have fixed the failure (a newer checker, a fixed test). Returns True if it rebased."""
        if not (mr.get("diverged_commits_count") or 0) > 0:
            return False
        try:
            head = self.gl.get(f"/projects/:id/repository/branches/{self.pol['project'].get('default_branch', 'main')}")["commit"]["id"]
        except Exception:
            return False
        if tl.get("rebased_onto") == head:
            return False
        self.store.set_timeline(f"{MR}-{mr['iid']}", rebased_onto=head)
        try:
            self.gl.put(f"/projects/:id/merge_requests/{mr['iid']}/rebase")
        except GitLabError as exc:
            self.log(f"!{mr['iid']}: rebase before the Medic failed: {exc}")
            return False
        self.notify.status(MR, mr["iid"], "Rebased on main", "done", note="before the Medic", title=mr["title"],
                           now="The pipeline failed; main has moved, so Vallum rebased first and runs the checks again.")
        self.log(f"!{mr['iid']}: failed but behind main: rebased instead of calling the Medic")
        return True

    def medic_gave_up(self, mr: dict, tl: dict):
        """The Medic already looked at this commit. If it answered that a person must fix it, hold the MR."""
        bot = (self.flows.registry("medic") or {}).get("service_account") if self.flows else None
        if not bot:
            return
        notes = [n for n in self.gl.notes(MR, mr["iid"]) if (n.get("author") or {}).get("username") == bot
                 and "Pipeline Medic" in (n.get("body") or "")]
        if not notes or "needs a human" not in notes[-1]["body"].lower():
            return
        body = notes[-1]["body"]
        cause = body.split("**Cause:**", 1)[1].split("·", 1)[0].strip() if "**Cause:**" in body else "see the Medic's note"
        self.hold_for_human(mr, f"The Pipeline Medic could not fix the failing pipeline: {cause[:400]}",
                            ["Fix it yourself and push", "Close the merge request"], note_id=notes[-1].get("id"))

    def member_mr(self, iid: int) -> bool:
        """Door B: the MR's first commit was written by a person, not by one of Vallum's agents."""
        commits = self.gl.paged(f"/projects/:id/merge_requests/{iid}/commits") or []
        if not commits:
            return False
        first = commits[-1]                                  # the API lists newest first
        email, name = (first.get("author_email") or "").lower(), first.get("author_name") or ""
        return not ("service_account" in email or name.startswith("Vallum "))

    def escalated(self, mr: dict) -> bool:
        """A person's decision comes before any automatic fixing: if the Reviewer's latest verdict on this MR is
        needs-human (and no one has released it), hold the MR now, whatever its pipeline says."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        if tl.get("held_for"):
            return not self.human_released(mr)
        review = verdicts.latest(self.gl.notes(MR, mr["iid"]), self.flows.bots()).get("review")
        if review and review.get("verdict") == "needs-human" and tl.get("released_for") != review.get("note_id"):
            self.hold_for_human(mr, review.get("decision_needed") or "The Reviewer asked for a person's decision.",
                                review.get("options") or [], note_id=review.get("note_id"))
            return True
        return False

    def send_medic(self, mr: dict, reason: str = "pipeline failed"):
        """A Vallum MR whose pipeline failed (or whose rebase conflicted): the Medic gets at most 2 attempts (B19)."""
        target = f"{MR}-{mr['iid']}"
        tl = self.store.timeline(target)
        if tl.get("held_for"):
            return
        if self.member_mr(mr["iid"]):
            # a member's own MR (Door B): Vallum never pushes fixes to it; the author decides what to change
            if tl.get("medic_sha") == mr["sha"]:
                return
            self.store.set_timeline(target, medic_sha=mr["sha"])
            self.notify.status(MR, mr["iid"], "Pipeline failed", "waiting", note=mr["sha"][:8], title=mr["title"],
                               now="The pipeline failed. This is your merge request, so Vallum doesn't change it.",
                               action="push a fix (the failing jobs are in the pipeline), or close the merge request.")
            return
        if reason == "pipeline failed" and self.rebase_if_main_moved(mr, tl):
            return
        if tl.get("medic_sha") == mr["sha"]:
            self.medic_gave_up(mr, tl)
            return
        attempts = int(tl.get("medic_attempts", 0))
        cap = int(self.pol["releases"]["max_fix_attempts"])
        fresh = self.gl.mr(mr["iid"])
        if fresh.get("sha") != mr["sha"] or (fresh.get("head_pipeline") or {}).get("status") in ("running", "pending", "created"):
            self.log(f"!{mr['iid']}: a new commit or pipeline arrived; not calling the Medic on the old failure")
            return
        if attempts >= cap or not self.pol.get("gate", {}).get("agent_verdicts_required"):
            self.store.set_timeline(target, medic_sha=mr["sha"])
            self.hold_for_human(mr, f"The {reason} and the Pipeline Medic already had {attempts} attempt(s).",
                                ["Fix the failing job yourself", "Close the merge request"])
            return
        fp = evidence.fingerprint(self.gl.mr_changes(mr["iid"]))
        if not self.start_agent(mr, "medic", "medic", fp, {"merge_request_iid": mr["iid"], "reason": reason,
                                                           "attempt": attempts + 1, "max_attempts": cap}):
            return
        self.store.set_timeline(target, medic_sha=mr["sha"], medic_attempts=attempts + 1)
        self.gl.mr_labels(mr["iid"], remove=[self.pol.label("ready")])
        self.notify.status(MR, mr["iid"], "Pipeline Medic", "running", note=f"attempt {attempts + 1} of {cap}",
                           title=mr["title"], now=f"The {reason} on {mr['sha'][:8]}; the Medic is reading the job logs.",
                           action="nothing.")

    # ------------------------------------------------------------------ the full check
    TRANSIENT = {6, 11, 12}   # stale rebase or pipeline, mode, queue: can change without a new commit

    def full_check(self, mr: dict, found: dict | None = None) -> dict:
        ctx = context.build(self.gl, mr, "desk")
        found = found or {}
        review, insp = found.get("review") or {}, found.get("inspector") or {}
        ctx.reviewer_verdict, ctx.reviewer_author = review.get("verdict"), review.get("author")
        ctx.inspector = insp or None
        ctx.fingerprint = evidence.fingerprint(self.gl.mr_changes(mr["iid"]))
        ctx.housekeeping = context.is_housekeeping(self.gl, mr, self.pol)
        ov = self.store.timeline(f"{MR}-{mr['iid']}").get("override")
        ctx.override_by = (ov or {}).get("by") or (
            "a label" if self.pol.label("override") in mr.get("labels", []) else None)
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
        for mr in ready:
            self.close_steps(mr["iid"], ["Pipeline Medic", "Checks"])
        main_head = self.main_head()
        refused = [m for m in ready if self.store.timeline(f"{MR}-{m['iid']}").get("refused_sha") == m["sha"]
                   and self.store.timeline(f"{MR}-{m['iid']}").get("refused_main") == main_head]
        held = [m for m in ready + failed if self.store.timeline(f"{MR}-{m['iid']}").get("held_for")
                and self.pol.label("needs_human") in m.get("labels", [])]
        ready = [m for m in ready if m not in refused and m not in held]
        paused, candidates = [], []
        for m in ready:
            why = self.freeze_reason(m)
            if why:
                paused.append((m, why))
                self.pause(m, why)
            else:
                self.unpause(m)
                candidates.append(m)
        candidates.sort(key=lambda m: priority.queue_key(self.level(m), m.get("created_at")))
        self.save_queue(candidates, waiting, failed, paused, held, refused)
        for mr in failed:
            if mr in held or self.escalated(mr):
                continue
            self.send_medic(mr)
        for pos, mr in enumerate(candidates[1:], 2):
            tl = self.store.timeline(f"{MR}-{mr['iid']}")
            if tl.get("queue_pos") != pos:
                self.store.set_timeline(f"{MR}-{mr['iid']}", queue_pos=pos)
                self.notify.status(MR, mr["iid"], "Queued", "waiting", note=f"position {pos}", title=mr["title"],
                                   now=f"In the release queue at position {pos}. One release at a time.")
        if not candidates:
            self.log(f"nothing ready ({len(waiting)} waiting, {len(failed)} failed, {len(refused)} refused, "
                     f"{len(paused)} paused, {len(held)} waiting for a human)")
            self.notify.ledger()
            return 0

        head = candidates[0]
        iid = head["iid"]
        self.log(f"next: !{iid} {head['title']}")
        self.gl.mr_labels(iid, add=[self.pol.label("ready")], remove=[self.pol.label("blocked")])
        self.notify.status(MR, iid, "Queued", "done", note="next", title=head["title"], priority=self.level(head),
                           issue_iid=evidence.related_issue(head.get("description")), now="Picked up by the Merge Desk.",
                           action="nothing.")
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
            failed_rules = "; ".join(f"rule {r['no']}: {r['detail']}" for r in report["rules"] if r["status"] not in ("pass", "waived"))
            if self.only_transient(report):
                self.notify.status(MR, iid, f"Gatekeeper {report['score']}", "waiting", tier=tier,
                                   now=f"Not merged yet: {failed_rules}. The desk tries again on its next run.", action="nothing.")
            else:
                self.store.set_timeline(f"{MR}-{iid}", refused_sha=mr["sha"], blocked_sha=mr["sha"],
                                        refused_main=self.main_head())
                self.notify.status(MR, iid, f"Gatekeeper {report['score']}", "failed", tier=tier,
                                   now=f"Not merged. {failed_rules}", action="fix the failed rules and push; the desk retries after a new commit.")
                self.gl.mr_labels(iid, add=[self.pol.label("blocked")], remove=[self.pol.label("ready")])
            self.notify.ledger()
            return 0

        release_id = self.store.next_release_id()
        feature = ", ".join(report["features"]) or "no feature change"
        deadline = utcnow() + timedelta(minutes=float(self.pol["watchdog"]["deadlines_min"]["merged"]))
        try:
            self.store.claim_release(("idle",), slot="merged", release_id=release_id, mr_iid=iid, sha=mr["sha"],
                                     feature=feature, features=report["features"], touched=report["touched"], tier=tier,
                                     author=mr["author"]["username"], title=mr["title"], merged_at=None,
                                     deadline=iso(deadline), restarted=False, abort=False,
                                     housekeeping=context.is_housekeeping(self.gl, mr, self.pol))
        except Abort as exc:
            self.log(f"could not claim the release slot: {exc}")
            return 0
        self.store.sanction(mr["sha"], mr_iid=iid, release_id=release_id)
        overrides = self.store.timeline(f"{MR}-{iid}").get("override")
        self.store.save_release(release_id, {
            "release": release_id, "mr": iid, "title": mr["title"], "requester": mr["author"]["username"],
            "feature": feature, "tier": tier, "started_at": iso(),
            "decisions": [
                {"by": "membership-check", "result": next(r["detail"] for r in report["rules"] if r["no"] == 1)},
                {"by": "gatekeeper", "rules": report["score"], "sha": mr["sha"], "at": iso()},
            ] + ([{"by": f"override (@{overrides.get('by')})", "result": "freeze overridden", "at": overrides.get("at")}]
                 if overrides else []),
            "human_actions": 1 if overrides else 0,
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
