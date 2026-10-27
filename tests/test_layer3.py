"""Layer 3: allowances and modes, the Cockpit, needs-human holds, project context for any codebase (T3.x)."""

import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from vallum import agents, allowance, cockpit, codemap, config, housekeeping
from vallum.checker import runner
from vallum.config import Policy, iso, utcnow
from vallum.flows import FLOWS, FLOWS_DIR, MENTION
from vallum.gate import rules
from vallum.gitlab import GitLab
from vallum.patrol.__main__ import CONFIRM_RUNS, Patrol, pause_tier
from vallum.state import MemoryState, Store
from tests.test_gate import ctx, statuses

ROOT = config.ROOT
EXPRESS = ROOT / "tests" / "fixtures" / "express-app"


def policy() -> Policy:
    return Policy(config.load_local("policy.yaml"))


class FakeNotify:
    def __init__(self):
        self.alerts, self.statuses = [], []

    def alert(self, kind, iid, event, **kw):
        self.alerts.append((kind, iid, event, kw))

    def status(self, kind, iid, step, state, **kw):
        self.statuses.append((kind, iid, step, state))

    def ledger(self):
        pass


# ---------------------------------------------------------------- allowance and modes (B10)
def test_allowance_math():
    targets = {"core": 0.999, "standard": 0.995, "experimental": 0.99, "app": 0.995}
    bucket = {"at": iso(), "counts": {"standard": {"visits": 2000, "bad": 5}, "app": {"visits": 2000, "bad": 5}},
              "spend": {"standard": 0.05}}
    got = allowance.compute([bucket], targets, 60)
    # 2000 visits at 99.5% allow 10 bad; 5 bad uses half; a broken live promise spends 5% more
    assert got["standard"]["allowance"] == pytest.approx(0.45)
    assert got["app"]["allowance"] == pytest.approx(0.5)
    assert got["core"]["allowance"] == 1.0                      # no visits, no mistakes
    old = {"at": iso(utcnow() - timedelta(minutes=90)), "counts": {"core": {"visits": 10, "bad": 10}}}
    assert allowance.compute([old], targets, 60)["core"]["allowance"] == 1.0   # outside the window


def test_noise_floor_keeps_one_slow_visit_from_flipping_core():
    """Regression: overnight, 1-2 slow redirects out of ~460 per patrol flipped Core to Red six times."""
    targets = {"core": 0.999, "standard": 0.995, "experimental": 0.99, "app": 0.995}
    blip = [{"at": iso(), "counts": {"core": {"visits": 2760, "bad": 2}}}]
    assert allowance.compute(blip, targets, 60)["core"]["allowance"] < 0.5                       # without a floor: Red
    assert allowance.compute(blip, targets, 60, min_allowed_bad=10)["core"]["allowance"] == pytest.approx(0.8)
    slip = [{"at": iso(), "counts": {"core": {"visits": 12000, "bad": 20}}}]                    # a real slip still turns it Red
    assert allowance.compute(slip, targets, 60, min_allowed_bad=10)["core"]["allowance"] < 0.5
    assert policy()["allowance"]["min_allowed_bad"] == 20
    six_blips = [{"at": iso(), "counts": {"core": {"visits": 2760, "bad": 6}}}]                # seen overnight on 24 Oct
    assert allowance.compute(six_blips, targets, 60, min_allowed_bad=20)["core"]["allowance"] == pytest.approx(0.7)
    assert allowance.compute(slip, targets, 60, min_allowed_bad=20)["core"]["allowance"] < 0.5


def test_ledger_counts_only_recent_rollbacks():
    from vallum.notify import render_ledger

    old, new = iso(utcnow() - timedelta(hours=20)), iso(utcnow() - timedelta(minutes=5))
    text = render_ledger({}, {"rollbacks": [old, new]}, {}, [], [], breaker_hours=6)
    assert "**Circuit breaker:** 1 rollback(s)" in text


def test_bad_visits_are_failures_or_very_slow():
    samples = [{"route": "GET /r/{code}", "ok": True, "ms": 50, "t": 10},
               {"route": "GET /r/{code}", "ok": True, "ms": 400, "t": 10},     # > 2 x 150 ms
               {"route": "GET /r/{code}", "ok": False, "ms": 5, "t": 10},
               {"route": "GET /r/{code}", "ok": False, "ms": 5, "t": 1},       # warm-up, not counted
               {"route": "GET /health", "ok": True, "ms": 5, "t": 10}]
    counts = allowance.count(samples, {"GET /r/{code}": "core", "GET /health": "standard"}, {"GET /r/{code}": 150},
                             slow_factor=2, warmup_s=3)
    assert counts["core"] == {"visits": 3, "bad": 2}
    assert counts["standard"] == {"visits": 1, "bad": 0}
    assert counts["app"] == {"visits": 4, "bad": 2}


def test_a_release_is_charged_only_for_its_own_bad_visits():
    """Regression (r-0006): slow visits on the old revision under the simulator's load pushed app-wide to Yellow."""
    samples = [{"route": "GET /r/{code}", "ok": True, "ms": 400, "t": 10, "revision": "new"},
               {"route": "GET /r/{code}", "ok": True, "ms": 400, "t": 10, "revision": "old"},
               {"route": "GET /r/{code}", "ok": True, "ms": 40, "t": 10, "revision": "old"}]
    counts = allowance.count(samples, {"GET /r/{code}": "core"}, {"GET /r/{code}": 150}, blame="new")
    assert counts["core"] == {"visits": 3, "bad": 1}


def test_rollback_waits_for_the_traffic_split_not_full_readiness():
    from vallum.drive.cloudrun import CloudRun, expected_split

    assert expected_split([{"revisionName": "old", "percent": 100}, {"revisionName": "new", "percent": 0, "tag": "cand"}]) == {"old": 100}

    class Session:
        def __init__(self):
            self.gets = 0

        def get(self, url):
            self.gets += 1
            done = self.gets >= 3
            body = {"metadata": {"generation": 5, "name": "vallum"}, "spec": {"traffic": []},
                    "status": {"observedGeneration": 5 if done else 4,
                               "traffic": [{"revisionName": "old" if done else "new", "percent": 100}],
                               "conditions": [{"type": "Ready", "status": "Unknown"}]}}
            return httpx.Response(200, json=body)

        def put(self, url, json=None):
            return httpx.Response(200, json=json)

    cr = CloudRun(Session(), "p", "r", "vallum")
    svc = cr.wait_traffic({"old": 100}, timeout=5, poll_s=0)
    assert CloudRun.serving(svc) == {"old": 100}                  # returned although Ready is still Unknown


def test_route_profile_reads_this_repos_cards():
    from vallum import cards as cardlib

    tier_of, limits = allowance.route_profile(cardlib.load_from_dir(ROOT).cards)
    assert tier_of["GET /r/{code}"] == "core"
    assert limits["GET /r/{code}"] > 0


def test_mode_hysteresis():
    tiers = policy()["tiers"]
    nm = allowance.next_mode
    assert nm("core", "green", 0.49, tiers) == "red"
    assert nm("core", "red", 0.60, tiers) == "red"                # must climb above 65% to leave Red
    assert nm("core", "red", 0.66, tiers) == "green"
    assert nm("standard", "green", 0.49, tiers) == "yellow"
    assert nm("standard", "yellow", 0.55, tiers) == "yellow"      # leaves Yellow only above 60%
    assert nm("standard", "yellow", 0.61, tiers) == "green"
    assert nm("standard", "yellow", 0.09, tiers) == "red"
    assert nm("standard", "red", 0.20, tiers) == "red"
    assert nm("standard", "red", 0.30, tiers) == "yellow"
    assert nm("experimental", "green", 0.0, tiers) == "green"


def test_update_modes_reports_changes_once():
    store, pol = Store(MemoryState()), policy()
    allowance.record(store, {"core": {"visits": 1000, "bad": 1000}, "app": {"visits": 1000, "bad": 1000}}, "test")
    changes = allowance.update_modes(store, pol)
    assert {(c["tier"], c["to"]) for c in changes} == {("core", "red"), ("app", "red")}
    assert store.doc("modes")["core"]["mode"] == "red"
    assert allowance.update_modes(store, pol) == []               # no change, no second alert


def test_mode_changes_are_announced():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    allowance.record(store, {"core": {"visits": 12000, "bad": 20}}, "drive r-1 1%")
    changes = allowance.update_modes(store, pol)
    allowance.announce(changes, store, notify, "owner")
    assert [a[2] for a in notify.alerts] == ["Core is now RED"]
    assert store.doc("recent")["events"][0]["kind"] == "mode"


def test_step_zero_collects_enough_for_app_wide_promises():
    from vallum.drive.__main__ import step0_seconds

    assert step0_seconds(12.0, {"GET /p/{code}": 10.8}, 160) == pytest.approx(160 * 1.3 / 10.8 + 2)
    assert step0_seconds(12.0, {"GET /r/{code}": 100.0}, 160) == 12.0


def test_a_release_judges_everything_changed_since_the_live_code():
    """r-0007 (link preview) would have redeployed the rolled-back redirect slip while judging only the preview."""
    from vallum.drive.__main__ import Drive

    class GL:
        def compare(self, a, b):
            assert a == "6ad46fd"
            return {"diffs": [{"new_path": "app/features/redirect.py"}, {"new_path": "app/features/link_preview.py"}]}

    drive = Drive.__new__(Drive)
    drive.pol, drive.gl, drive.store = policy(), GL(), Store(MemoryState())
    from vallum import cards as cardlib
    drive.cards = cardlib.load_from_dir(ROOT)
    drive.store.set_release(last_good="vallum-6ad46fd-2981156911")
    info = {"release_id": "r-7", "commit": "80b955f", "features": ["link-preview"], "touched": [], "tier": "standard"}
    out = drive.widen(info)
    assert "redirect" in out["touched"] and out["tier"] == "core" and out["carried"] == ["redirect"]


def test_carried_requests_are_reported_and_closed():
    """The preview (!14, issue #8) was refused at step 0 and later went live with the fix: tell its owner."""
    from vallum.drive.__main__ import Drive

    class GL:
        def __init__(self):
            self.notes, self.edits = [], []

        def add_note(self, kind, iid, body):
            self.notes.append((kind, iid))

        def edit_issue(self, iid, **kw):
            self.edits.append((iid, kw.get("state_event")))

        def mr(self, iid):
            return {"description": "Related to #8"}

    drive = Drive.__new__(Drive)
    drive.pol, drive.gl, drive.store = policy(), GL(), Store(MemoryState())
    drive.store.save_release("r-0007", {"release": "r-0007", "mr": 14, "title": "Link preview page",
                                        "feature": "link-preview", "outcome": "refused at step 0"})
    drive.store.save_release("r-0006", {"release": "r-0006", "feature": "redirect", "outcome": "rolled back"})
    drive.release_carried({"carried": ["link-preview", "redirect"]}, "r-0009", "vallum-x-1")
    assert ("issues", 8) in drive.gl.notes and (8, "close") in drive.gl.edits
    assert drive.store.load_release("r-0007")["released_in"] == "r-0009"
    drive.release_carried({"carried": ["link-preview"]}, "r-0010", "vallum-y-1")
    assert len(drive.gl.edits) == 1                                # reported once


def test_guard_promises_are_only_enough_for_fixes():
    """Regression (!13): a slowdown fix needs a behavior promise (catalog) that cannot fail on main (fail-first)."""
    from vallum import cards as cardlib
    from vallum.checker.__main__ import guards_allowed

    cs = cardlib.CardSet(cards={"redirect": {"feature": "redirect", "kind": "new-endpoint"},
                                "redirect@6-redirect": {"feature": "redirect", "kind": "bug-fix"},
                                "link-preview": {"feature": "link-preview", "kind": "ui-page"}})
    assert guards_allowed(cardlib.CardSet(cards={"x": {"feature": "x", "kind": "bug-fix"}}), [{"feature": "x"}])
    assert not guards_allowed(cs, [{"feature": "link-preview"}])
    card = yaml.safe_load(CARD_WITH_WATCH)
    card.update(issue=9, kind="bug-fix", priority="P0", flag=None)
    card["promises"]["behavior"][0]["guard"] = True
    assert [p for p in cardlib.validate(card, config.load_local("catalog.yaml")) if "guard" in p] == []


def test_local_load_tags_a_popular_link():
    """Regression (!13): the learned promise (popular == 1) was 'collecting' in every MR pipeline."""
    from vallum.checker import core

    class Client:
        def post(self, path, json=None, headers=None):
            return httpx.Response(201, json={"code": f"c{len(json['url'])}"})

        def get(self, path):
            return httpx.Response(302, headers={"x-revision": "local"})

    samples = core.local_load(Client(), {"GET /r/{code}"}, 10, "k")
    assert sum(1 for s in samples if s.get("popular") == 1) == 10
    assert sum(1 for s in samples if s.get("popular") == 0) == 10


def test_a_job_running_older_code_stands_down(monkeypatch):
    class GL:
        def __init__(self, head):
            self.head = head

        def branch_head(self, branch="main"):
            return self.head

    monkeypatch.setenv("CI_COMMIT_SHA", "aaa")
    assert config.running_stale_code(GL("bbb")) == "bbb"
    assert config.running_stale_code(GL("aaa")) is None
    monkeypatch.delenv("CI_COMMIT_SHA")
    assert config.running_stale_code(GL("bbb")) is None             # outside CI: always act


def test_a_small_first_step_carries_a_promise_still_collecting():
    """r-0011: at 1% a popular-only promise had 90 of 100 visits after 4 minutes and the release was rolled back."""
    from vallum.drive import judge

    rows = [{"verdict": judge.COLLECTING, "severity": "blocker"}]
    # r-0017: carrying at the end of the watch carried eight promises at 1%; they keep collecting until twice the watch
    assert judge.overall(rows, "core", 120, 120, final=True, carry=True) == judge.COLLECTING
    assert judge.overall(rows, "core", 240, 120, final=True, carry=True) == judge.KEPT
    assert judge.overall(rows, "core", 120, 120, final=True) == judge.COLLECTING
    assert judge.overall(rows, "core", 240, 120, final=True) == judge.AT_RISK
    assert judge.overall([{"verdict": judge.BROKEN, "severity": "blocker"}], "core", 120, 120, True, carry=True) == judge.BROKEN
    # a promise near its limit still rolls back at once at a small step (the Inspector asked on !18)
    assert judge.overall([{"verdict": judge.AT_RISK, "severity": "blocker"}], "core", 60, 120, False, carry=True) == judge.AT_RISK
    assert judge.overall(rows + [{"verdict": judge.AT_RISK, "severity": "blocker"}], "core", 240, 120, True,
                         carry=True) == judge.AT_RISK


def test_a_rollback_is_done_when_visitors_get_the_old_revision():
    from vallum.drive.__main__ import wait_serving

    answers = iter(["new"] * 5 + ["old"] * 3 + ["new"] + ["old"] * 20)
    t = {"now": 0.0}
    ok = wait_serving("https://x", "old", get=lambda u: next(answers), sleep=lambda s: t.update(now=t["now"] + s),
                      clock=lambda: t["now"])
    assert ok and round(t["now"], 1) == round(0.3 * 23, 1)          # 15 in a row only after the last stray answer
    assert wait_serving("https://x", "old", timeout=3, get=lambda u: "new",
                        sleep=lambda s: t.update(now=t["now"] + s), clock=lambda: t["now"]) is False


def test_a_promotion_is_confirmed_on_the_live_app_and_resent_once(monkeypatch):
    """r-0017 said 'live for everyone' while the 50% split served for another 50 s."""
    from vallum.drive import __main__ as drive

    sent, answers = [], iter([False, True])

    class CR:
        def set_traffic(self, traffic):
            sent.append(traffic)

    d = drive.Drive.__new__(drive.Drive)
    d.cr, d.url = CR(), "https://x"
    monkeypatch.setattr(drive, "wait_serving", lambda url, rev: next(answers))
    assert d.confirm_live([{"revisionName": "new", "percent": 100}], "new") is True
    assert len(sent) == 2                                         # not confirmed at first: sent once more


def test_watchdog_recovers_a_crashed_drive_at_once():
    from vallum.patrol.watchdog import decide

    rel = {"slot": "driving", "deadline": iso(utcnow() + timedelta(minutes=30))}
    assert decide(rel) is None
    assert decide(rel, drive_ended=True) == "rollback"


def test_watchdog_reads_the_slot_again_before_calling_a_drive_dead():
    """A drive that promoted frees the slot just before its job ends; that must not look like a crash."""
    from vallum.patrol.watchdog import Watchdog

    class GL:
        def get(self, path, **kw):
            return [{"name": "build-image", "status": "success"}, {"name": "test-drive", "status": "success"}]

    class St:
        def __init__(self, slot):
            self.slot = slot

        def release(self):
            return {"slot": self.slot, "release_id": "r-1", "drive_pipeline": 7}

    held = {"slot": "driving", "release_id": "r-1", "drive_pipeline": 7}
    assert Watchdog(GL(), St("driving"), None, None, None).drive_ended(held) == "success"
    assert Watchdog(GL(), St("idle"), None, None, None).drive_ended(held) is None


def test_yellow_adds_the_extra_step():
    pol = policy()
    assert pol.steps("standard", "green") == [10, 50, 100]
    assert pol.steps("standard", "yellow") == [10, 25, 50, 100]


# ---------------------------------------------------------------- Cockpit (B23)
def test_cockpit_parses_exact_commands_only():
    assert cockpit.parse("@vallum status") == [("status", "")]
    assert cockpit.parse("please @vallum ship-anyway !12 thanks\n@vallum pause") == [("ship-anyway", "!12 thanks"), ("pause", "")]
    assert cockpit.parse("say `@vallum pause` to pause") == []                   # quoted code is not a command
    assert cockpit.parse("```\n@vallum rollback links\n```") == []
    assert cockpit.parse("@vallum deploy everything") == []                      # not a command
    assert cockpit.parse_ref("!12") == ("merge_requests", 12)
    assert cockpit.parse_ref("#7") == ("issues", 7)


def test_commands_accept_the_cockpit_bots_name():
    names = ("vallum", "ai-vallum-cockpit-gitlab-ai-hackathon")
    assert cockpit.parse("@ai-vallum-cockpit-gitlab-ai-hackathon pause", names) == [("pause", "")]
    assert cockpit.parse("@ai-vallum-ship-gitlab-ai-hackathon pause", names) == []      # other bots are not the Cockpit
    assert cockpit.parse("@vallum listen 120", names) == [("listen", "120")]


def test_listen_mode_keeps_scanning_then_stops():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    gl = CockpitGL([cnote(1, 10, "@vallum listen 30")], access={10: 30})
    pit = cockpit.Cockpit(gl, store, pol, notify)
    now = {"t": 0.0}
    scans = {"n": 0}
    real_run = pit.run

    def counting_run():
        scans["n"] += 1
        return real_run()

    pit.run = counting_run
    refreshed = []
    notify.ledger = lambda: refreshed.append(scans["n"])
    pit.listen(budget_s=60, poll_s=15, sleep=lambda s: now.__setitem__("t", now["t"] + s), clock=lambda: now["t"])
    assert scans["n"] == 4                                        # 0 s, 15 s, 30 s, 45 s
    assert refreshed[:1] == [1]                                   # the command's effect shows on the Ledger at once
    assert "Listen mode:** on until" in pit.do_status("issues", 9, "", "user10")
    pit.do_listen("issues", 9, "off", "user10")
    scans["n"] = 0
    pit.listen(budget_s=60, poll_s=15, sleep=lambda s: None, clock=lambda: 0.0)
    assert scans["n"] == 1                                        # listen mode off: one scan only


def test_a_command_is_claimed_once_even_with_two_scanners():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    gl = CockpitGL([cnote(1, 10, "@vallum pause")], access={10: 30})
    a, b = cockpit.Cockpit(gl, store, pol, notify), cockpit.Cockpit(gl, store, pol, notify)
    assert a.claim("1:pause") is True and b.claim("1:pause") is False


def test_tighten_also_updates_the_plain_english_text():
    """Regression: MR !7 changed the limit to 250 ms but left `says` at 300 ms; GitLab Duo requested changes."""
    m = "p95(latency, route='GET /api/links/{code}') < 300ms"
    assert cockpit.tightened_says("95% of link lookups finish in under 300 ms", m, "250ms") == \
        "95% of link lookups finish in under 250 ms"
    assert cockpit.tightened_says("Lookups are quick", m, "250ms") == "Lookups are quick (limit 250ms)"
    assert cockpit.tightened_says("Under 0.5% fail", "error_rate(route='x') < 0.5%", "0.2%") == "Under 0.2% fail"
    assert cockpit.tightened_says("95% finish in 1300 ms", m, "250ms") == "95% finish in 1300 ms (limit 250ms)"


class WatchGL:
    def __init__(self):
        self.mrs = [{"iid": 9, "sha": "a", "detailed_merge_status": "requested_changes",
                     "labels": ["vallum", "vallum::needs-human"], "reviewers": [{"id": 5}]}]
        self.jobs = [{"id": 100, "name": "merge-desk", "status": "success"}, {"id": 99, "name": "live-patrol", "status": "running"}]
        self.retried = []

    def open_mrs(self, label):
        return self.mrs

    def get(self, path, **params):
        if path.endswith("/pipelines"):
            return [{"id": 7, "status": getattr(self, "pipe", "success")}]
        return self.jobs

    def post(self, path, json=None):
        self.retried.append(path)
        return {"id": 101}


def test_watcher_acknowledges_a_released_hold_and_runs_the_desk_at_once():
    """User feedback: after removing vallum::needs-human nothing visible happened until the next schedule."""
    from vallum.watch import Watcher

    store, pol, notify, gl = Store(MemoryState()), policy(), FakeNotify(), WatchGL()
    store.set_timeline("merge_requests-9", held_for=77)
    clock = {"t": 1000.0}
    w = Watcher(gl, store, pol, notify, log=lambda m: None, pipeline_id="555", clock=lambda: clock["t"])
    assert w.tick() is False                                      # first look only remembers
    gl.mrs[0]["labels"] = ["vallum"]                              # the owner removed the hold
    assert w.tick() is True
    assert ("merge_requests", 9, "Needs a human", "done") in notify.statuses
    assert gl.retried == ["/projects/:id/jobs/100/retry"]
    gl.mrs[0]["sha"] = "b"                                        # another change 30 s later: no second kick yet
    clock["t"] += 30
    assert w.tick() is True and len(gl.retried) == 1
    gl.jobs[0]["status"] = "running"
    clock["t"] += 300
    gl.mrs[0]["sha"] = "c"
    assert w.tick() is True and len(gl.retried) == 1              # the desk is already running
    gl.jobs[0]["status"] = "success"
    clock["t"] += 15
    assert w.tick() is False and len(gl.retried) == 2             # the deferred change still wakes the desk
    clock["t"] += 300
    assert w.tick() is False and len(gl.retried) == 2             # once


def test_a_finished_pipeline_wakes_the_desk():
    from vallum.watch import Watcher

    store, pol, notify, gl = Store(MemoryState()), policy(), FakeNotify(), WatchGL()
    w = Watcher(gl, store, pol, notify, log=lambda m: None, pipeline_id="555", clock=lambda: 10_000.0)
    w.tick()
    gl.pipe = "failed"                                            # same commit, same merge state, pipeline failed
    assert w.tick() is True and gl.retried == ["/projects/:id/jobs/100/retry"]


def test_needs_human_beats_the_medic_on_a_failed_pipeline():
    """Regression (MR !10): the Reviewer said needs-human but the pipeline had failed, so the desk went the Medic
    way and never held the MR."""
    from vallum.desk.__main__ import Desk

    class GL(LabelGL):
        def notes(self, kind, iid, limit=500):
            return [{"id": 77, "author": {"username": "ai-vallum-ship-gitlab-ai-hackathon"},
                     "body": '🛡 Vallum: **Reviewer verdict** · needs-human\n```json\n{"verdict": "needs-human", "sha": "abc", '
                             '"decision_needed": "90 days or never?", "options": ["90 days", "never"]}\n```',
                     "created_at": "2026-10-24T16:12:49Z"}]

    class Flows:
        def bots(self):
            return {"ai-vallum-ship-gitlab-ai-hackathon": "ship"}

    desk = Desk.__new__(Desk)
    desk.store, desk.pol, desk.notify, desk.gl, desk.flows = Store(MemoryState()), policy(), FakeNotify(), GL(), Flows()
    mr = {"iid": 10, "title": "x", "description": "", "labels": ["vallum"], "sha": "abc"}
    assert desk.escalated(mr) is True
    assert desk.gl.mr_calls[0][1] == ["vallum::needs-human"]
    assert any(a[2] == "Needs a human" for a in desk.notify.alerts)
    held = {**mr, "labels": ["vallum", "vallum::needs-human"]}
    assert desk.escalated(held) is True                            # still held: no Medic
    assert desk.escalated(mr) is False                             # the owner removed the label: carry on


def test_no_medic_on_a_members_own_mr():
    """Regression (MR !10): after the owner released the hold and decided to close, the desk sent the Medic to
    fix a member's own MR."""
    from vallum.desk.__main__ import Desk

    class GL(LabelGL):
        def paged(self, path, **kw):
            return [{"author_name": "Vallum Builder", "author_email": "service_account_x@noreply.gitlab.com"},
                    {"author_name": "Ansh Varshney", "author_email": "varshney.ansh04@gmail.com"}]   # newest first

    desk = Desk.__new__(Desk)
    desk.store, desk.pol, desk.notify, desk.gl = Store(MemoryState()), policy(), FakeNotify(), GL()
    assert desk.member_mr(10) is True
    desk.send_medic({"iid": 10, "sha": "abc", "title": "x", "diverged_commits_count": 0})
    assert ("merge_requests", 10, "Pipeline failed", "waiting") in desk.notify.statuses
    assert desk.store.timeline("merge_requests-10").get("medic_attempts") is None


def test_mr_activity_switches_listen_mode_on():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    pit = cockpit.Cockpit(CockpitGL([], access={}), store, pol, notify)
    assert pit.listening_until() is None
    pit.listen(budget_s=0, poll_s=15, sleep=lambda s: None, clock=lambda: 0.0, tick=lambda: True)
    assert pit.listening_until() > utcnow()


def test_tighten_only_makes_limits_stricter():
    m = "p95(latency, route='GET /r/{code}', popular == 1) < 150ms"
    assert cockpit.tightened(m, "120ms") == "p95(latency, route='GET /r/{code}', popular == 1) < 120ms"
    with pytest.raises(ValueError):
        cockpit.tightened(m, "200ms")
    assert cockpit.tightened("error_rate(route='GET /r/{code}') < 0.5%", "0.2%").endswith("< 0.2%")


class CockpitGL:
    def __init__(self, notes, access):
        self.items = {("issues", 9): notes}
        self.access = access
        self.added = []

    def recent(self, kind, since):
        return [{"iid": iid} for (k, iid) in self.items if k == kind]

    def notes(self, kind, iid, limit=500):
        return self.items.get((kind, iid), [])[-limit:]

    def member_access(self, user_id):
        return self.access.get(user_id, 0)

    def add_note(self, kind, iid, body):
        self.added.append((kind, iid, body))

    def edit_issue(self, iid, **kw):
        pass

    def mr_labels(self, iid, add=(), remove=()):
        pass


def cnote(nid, user_id, body, minutes_ago=1):
    return {"id": nid, "system": False, "body": body, "author": {"id": user_id, "username": f"user{user_id}"},
            "created_at": iso(utcnow() - timedelta(minutes=minutes_ago))}


def test_cockpit_runs_member_commands_once_and_refuses_others():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    gl = CockpitGL([cnote(1, 10, "@vallum pause"), cnote(2, 99, "@vallum resume")], access={10: 30})
    done = cockpit.Cockpit(gl, store, pol, notify).run()
    assert done == ["pause by user10", "refused resume from user99"]
    assert store.doc("meta")["safe_mode"] is True                 # the stranger's resume did nothing
    assert "refused" in gl.added[1][2]
    audit = store.load_release("commands")["decisions"]
    assert [d["command"] for d in audit] == ["pause", "resume"]
    assert cockpit.Cockpit(gl, store, pol, notify).run() == []     # each comment is carried out once


def test_cockpit_runs_a_command_added_by_an_edit_once():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    first = cnote(1, 10, "@vallum status")
    gl = CockpitGL([first], access={10: 30})
    assert cockpit.Cockpit(gl, store, pol, notify).run() == ["status by user10"]
    store.update(cockpit.DOC, lambda cur: {**cur, "last_scan": iso(utcnow() - timedelta(minutes=5))})
    edited = {**first, "body": "@vallum status\n@vallum pause", "updated_at": iso()}
    gl.items[("issues", 9)] = [edited]
    assert cockpit.Cockpit(gl, store, pol, notify).run() == ["pause by user10"]   # status is not run again


def test_notes_are_the_newest_ones_in_order():
    calls = {}

    def handler(request):
        calls["params"] = dict(request.url.params)
        return httpx.Response(200, json=[{"id": 3}, {"id": 2}, {"id": 1}])

    gl = GitLab(project_id="1", api_url="https://gitlab.example/api/v4")
    gl.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert [n["id"] for n in gl.notes("issues", 3)] == [1, 2, 3]
    assert calls["params"]["sort"] == "desc"                      # regression: the Ledger had 610 notes


def test_a_comment_missed_by_a_lagging_scan_is_still_carried_out():
    """Regression: `@vallum resume` was posted 2 s before a scan that read a lagging replica; the next scans
    only looked after that scan's start, so the command was never seen."""
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    store.update(cockpit.DOC, lambda cur: {**cur, "last_scan": iso(utcnow() - timedelta(seconds=20))})
    late = cnote(5, 10, "@vallum resume", minutes_ago=5)                 # older than the last scan
    gl = CockpitGL([late], access={10: 30})
    assert cockpit.Cockpit(gl, store, pol, notify).run() == ["resume by user10"]
    assert cockpit.Cockpit(gl, store, pol, notify).run() == []          # and only once


def test_explain_does_not_read_its_own_note_back_as_a_command():
    """Regression: `@vallum explain !5` posted `@<cockpit bot> explain !5 ...`, which was read back as a new
    command every 15 s (a loop of Cockpit sessions)."""
    class Flows:
        def registry(self, key):
            return {"service_account": "ai-vallum-cockpit-gitlab-ai-hackathon"}

        def bots(self):
            return {"ai-vallum-cockpit-gitlab-ai-hackathon": "cockpit"}

    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    gl = CockpitGL([cnote(1, 10, "@vallum explain !5")], access={10: 30})
    pit = cockpit.Cockpit(gl, store, pol, notify, Flows())
    assert pit.run() == ["explain by user10"]
    asked = [b for _k, _i, b in gl.added if "please explain" in b][0]
    gl.items[("issues", 9)].append(cnote(2, 10, asked))            # the note code posted, as the owner
    assert pit.run() == []
    assert cockpit.is_ours("@ai-vallum-cockpit-gitlab-ai-hackathon explain !5 for @x (asked with `@vallum explain`)")


def test_cockpit_ship_anyway_and_resume():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    store.update("meta", lambda cur: {**cur, "safe_mode": True, "rollbacks": ["x"]})
    gl = CockpitGL([cnote(1, 10, "@vallum ship-anyway !12"), cnote(2, 10, "@vallum resume")], access={10: 40})
    cockpit.Cockpit(gl, store, pol, notify).run()
    assert store.timeline("merge_requests-12")["override"]["by"] == "user10"
    assert store.doc("meta")["safe_mode"] is False and store.doc("meta")["rollbacks"] == []


def test_override_passes_the_mode_rule_and_housekeeping_skips_agent_review():
    red_core = {"core": {"mode": "red"}}
    files = ["app/features/redirect.py", "promises/features/base-redirect.yaml"]
    assert statuses(rules.evaluate(ctx(files=files, card=None, modes=red_core)))[11] == "fail"
    assert statuses(rules.evaluate(ctx(files=files, card=None, modes=red_core, override_by="user10")))[11] == "pass"
    c = ctx(files=["docs/vallum/CODEMAP.md"], card=None, housekeeping=True, reviewer_verdict=None, inspector=None)
    c.policy.data["gate"]["agent_verdicts_required"] = True
    assert statuses(rules.evaluate(c))[9] == "waived"


# ---------------------------------------------------------------- agents started by triggers, credit guard
class FakeFlows:
    def __init__(self, trigger_ok=True, trigger_raises=False):
        self.trigger_ok, self.trigger_raises = trigger_ok, trigger_raises
        self.api = []

    def start_by_trigger(self, key, kind, iid, as_reviewer):
        if self.trigger_raises:
            raise RuntimeError("cannot assign")
        return self.trigger_ok

    def start(self, key, goal, issue_id=None):
        self.api.append(key)
        return {"id": 1}


def test_agents_start_by_trigger_then_fall_back_and_respect_the_credit_guard():
    store, pol, notify = Store(MemoryState()), policy(), FakeNotify()
    assert agents.start(FakeFlows(), store, pol, notify, "release", "issues", 4, {}) == "trigger"
    fallback = FakeFlows(trigger_raises=True)
    assert agents.start(fallback, store, pol, notify, "release", "issues", 4, {}) == "api" and fallback.api == ["release"]
    day = utcnow().strftime("%Y-%m-%d")
    assert store.doc("usage")[day] == 2
    pol.data["credits"]["daily_code_started_sessions"] = 2
    assert agents.start(FakeFlows(), store, pol, notify, "release", "issues", 4, {}) is None
    assert store.doc("meta")["safe_mode"] is True and "credit guard" in store.doc("meta")["safe_mode_reason"]


# ---------------------------------------------------------------- Live Patrol
def test_live_break_needs_two_patrols_before_an_incident():
    from vallum.cards import Promise

    patrol = Patrol.__new__(Patrol)
    patrol.store, patrol.pol = Store(MemoryState()), policy()
    opened = []
    patrol.live_incident = lambda b, rev, entry: opened.append(b["promise"].id) or 77
    broken = [{"promise": Promise("redirect-works", "behavior", {"says": "x"}, "redirect", "card"), "detail": "500", "tier": "core"}]
    patrol.report_broken(broken, "rev-1")
    assert opened == [] and CONFIRM_RUNS == 2
    patrol.report_broken(broken, "rev-1")
    assert opened == ["redirect-works"]
    patrol.report_broken(broken, "rev-1")
    assert opened == ["redirect-works"]                            # one incident per break
    patrol.report_broken([], "rev-1")
    assert patrol.store.doc("patrol")["broken"] == {}              # recovered: the count starts over


def test_pause_tier_comes_from_the_directors_note():
    bot = "ai-vallum-ship-bot"
    notes = [{"author": {"username": "stranger"}, "body": '{"action": "pause", "tier": "experimental"}'},
             {"author": {"username": bot}, "body": '```json\n{"step": "plan", "action": "pause", "tier": "core"}\n```'}]
    assert pause_tier(notes, bot) == "core"
    assert pause_tier(notes[:1], bot) is None


# ---------------------------------------------------------------- bug regressions
def test_ledger_resource_lock_is_retried(monkeypatch):
    """Regression: the Live Patrol and the Merge Desk editing the Ledger at once got 409 Resource lock."""
    monkeypatch.setattr("vallum.gitlab.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(409, json={"message": "Resource lock"})
        return httpx.Response(200, json={"id": 5})

    gl = GitLab(project_id="1", api_url="https://gitlab.example/api/v4")
    gl.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert gl.put("/projects/:id/issues/1/notes/5", json={"body": "x"})["id"] == 5
    assert calls["n"] == 2


# ---------------------------------------------------------------- project context for any codebase (B29, T3.8)
def test_codemap_of_this_repo_is_structural():
    text = codemap.generate(ROOT)
    assert text.startswith("# Code map: ")
    for section in ("## Layout", "## Features (from Feature Cards)", "## Modules"):
        assert section in text
    from vallum import cards as cardlib

    for name in cardlib.load_from_dir(ROOT).cards:
        assert f"| {name} |" in text
    for path in sorted((ROOT / "app" / "features").glob("*.py")):
        assert f"`app/features/{path.name}`" in text
    # the committed map may lag one release behind (housekeeping refreshes it), so only its shape is checked
    committed = (ROOT / "docs" / "vallum" / "CODEMAP.md").read_text(encoding="utf-8")
    assert committed.startswith("# Code map: ") and "## Modules" in committed


def test_codemap_works_for_an_express_app():
    text = codemap.generate(EXPRESS)
    assert "javascript" in text and "express" in text.lower()
    assert "GET /orders/:id" in text or "GET /orders/{id}" in text
    assert "orders" in text
    assert "src/db.js" in text


BANNED = re.compile(r"alias|shorten|/r/|fastapi|uvicorn|app/features|links\.py|custom_alias|promo\b", re.I)


@pytest.mark.parametrize("path", sorted(str(p.relative_to(ROOT)) for p in
                                         list((ROOT / "skills").glob("*/SKILL.md")) + list(FLOWS_DIR.glob("*.yaml"))))
def test_skills_and_flows_are_project_neutral(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = sorted({m.group(0) for m in BANNED.finditer(text)})
    assert not hits, f"{path} names this project ({hits}); project details belong in docs/vallum/ or vallum/project.yaml"


def test_every_flow_reads_guidelines_codemap_and_role_notes():
    for key, spec in FLOWS.items():
        text = (FLOWS_DIR / spec["file"]).read_text(encoding="utf-8")
        if key == "inspect":   # independent by design: no tools, only the code-built evidence bundle
            assert "toolset: []" in text
            continue
        assert "docs/vallum/GUIDELINES.md" in text and "docs/vallum/CODEMAP.md" in text, key
        assert "docs/vallum/roles/" in text, key
    assert MENTION in FLOWS["cockpit"]["triggers"] and FLOWS["cockpit"]["triggers"][MENTION]["active"]


def test_project_specific_files_are_protected_and_profile_is_complete():
    pol = policy()
    assert "docs/vallum/GUIDELINES.md" in pol["protected_paths"] and "docs/vallum/roles/" in pol["protected_paths"]
    profile = yaml.safe_load((ROOT / "vallum" / "project.yaml").read_text(encoding="utf-8"))
    for key in ("language", "source_dirs", "test_dirs", "feature", "commands", "never_edit"):
        assert key in profile


def test_checker_starts_the_app_from_the_profile():
    start, health = runner.app_commands({"commands": {"start": "node src/app.js --port {port}", "health": "/healthy"}})
    assert runner.start_argv(start, 8123) == ["node", "src/app.js", "--port", "8123"]
    assert health == "/healthy"
    start, health = runner.app_commands()
    argv = runner.start_argv(start, 9000)
    assert argv[0] and "9000" in argv and health == "/health"


# ---------------------------------------------------------------- housekeeping after a release
CARD_WITH_WATCH = """feature: demo
tier: standard
promises:
  behavior:
    - id: demo-works
      says: "It works"
      steps: ["GET /demo -> 200"]
      where: [mr]
      severity: blocker
watch:
  - id: demo-popular-fast
    type: speed
    says: "Popular items stay fast"
    measure: "p95(latency, route='GET /demo', popular == 1) < 150ms"
    where: [drive]
    severity: blocker
    raised_by: reviewer
    concern: "cache"

owns:
  routes: ["GET /demo"]
"""


def test_drop_watch_removes_only_the_watch_block():
    new = housekeeping.drop_watch(CARD_WITH_WATCH)
    assert new is not None and "watch:" not in new
    card = yaml.safe_load(new)
    assert card["owns"]["routes"] == ["GET /demo"] and card["promises"]["behavior"][0]["id"] == "demo-works"
    assert housekeeping.drop_watch(new) is None                    # nothing left to drop
    at_end = CARD_WITH_WATCH.split("\nowns:")[0] + "\n"
    assert "watch" not in yaml.safe_load(housekeeping.drop_watch(at_end))


def test_housekeeping_changes_only_when_needed(tmp_path):
    text = codemap.generate(ROOT)
    files = {"docs/vallum/CODEMAP.md": text, "promises/features/9-demo.yaml": CARD_WITH_WATCH}
    actions = housekeeping.changes(ROOT, ["promises/features/9-demo.yaml"], files.get)
    assert [a["file_path"] for a in actions] == ["promises/features/9-demo.yaml"]
    assert actions[0]["dropped"] == ["demo-popular-fast"]
    shifted = re.sub(r" :(\d+)", lambda m: f" :{int(m.group(1)) + 3}", text)   # only line numbers moved
    shifted = re.sub(r", (\d+) lines\)", lambda m: f", {int(m.group(1)) + 1} lines)", shifted)
    assert housekeeping.changes(ROOT, [], {"docs/vallum/CODEMAP.md": shifted}.get) == []
    assert housekeeping.changes(ROOT, [], {}.get)[0]["action"] == "create"


def test_housekeeping_map_never_lists_the_watch_it_drops(tmp_path):
    """!15: the code map was generated before the expired watch was dropped, so it still listed it (GitLab Duo)."""
    (tmp_path / "promises" / "features").mkdir(parents=True)
    (tmp_path / "vallum").mkdir()
    (tmp_path / "vallum" / "project.yaml").write_text("name: demo\n", encoding="utf-8")
    (tmp_path / "promises" / "features" / "9-demo.yaml").write_text(CARD_WITH_WATCH, encoding="utf-8")
    assert "demo-popular-fast" in codemap.generate(tmp_path)
    actions = housekeeping.changes(tmp_path, ["promises/features/9-demo.yaml"],
                                   {"promises/features/9-demo.yaml": CARD_WITH_WATCH}.get)
    by_path = {a["file_path"]: a for a in actions}
    assert "demo-popular-fast" not in by_path["docs/vallum/CODEMAP.md"]["content"]
    assert by_path["promises/features/9-demo.yaml"]["dropped"] == ["demo-popular-fast"]


def test_housekeeping_mr_names_what_expired_and_sends_no_extra_keys():
    posts = []

    class GL:
        def open_mrs(self, label):
            return []

        def post(self, path, json=None):
            posts.append((path, json))
            return {"iid": 99}

    actions = [{"action": "update", "file_path": "promises/features/9-demo.yaml", "content": "x",
                "dropped": ["demo-popular-fast"]}]
    housekeeping.open_mr(GL(), config.policy(), "r-0099", actions)
    commit, mr = posts[0][1], posts[1][1]
    assert all(set(a) == {"action", "file_path", "content"} for a in commit["actions"])
    assert "`demo-popular-fast` in `9-demo.yaml`" in mr["description"] and "docs-only" not in mr["description"]


def test_a_housekeeping_mr_with_requested_changes_is_made_again_once(monkeypatch):
    from vallum.desk.__main__ import Desk

    made, held, closed = [], [], []
    timelines = {}

    class St:
        def timeline(self, t):
            return timelines.get(t, {})

        def set_timeline(self, t, **kw):
            timelines.setdefault(t, {}).update(kw)

        def event(self, *a, **k):
            pass

    class GL:
        def put(self, path, json=None):
            closed.append(path)

        def add_note(self, *a):
            pass

        def file_raw_or_none(self, p, ref="main"):
            return CARD_WITH_WATCH

    desk = Desk.__new__(Desk)
    desk.gl, desk.store, desk.pol, desk.log = GL(), St(), config.policy(), lambda m: None
    desk.hold_for_human = lambda mr, q, opts, note_id=None: held.append(mr["iid"])
    monkeypatch.setattr("vallum.desk.__main__.context.is_housekeeping", lambda gl, mr, pol: True)
    monkeypatch.setattr("vallum.desk.__main__.context.mr_changes",
                        lambda gl, iid: (["docs/vallum/CODEMAP.md", "promises/features/9-demo.yaml"], 5, []))
    monkeypatch.setattr("vallum.desk.__main__.housekeeping.changes", lambda root, cards, f: [{"file_path": c} for c in cards])
    monkeypatch.setattr("vallum.desk.__main__.housekeeping.open_mr",
                        lambda gl, pol, rid, actions, why=None: made.append((rid, actions, why)) or {"iid": 16})

    mr = {"iid": 15, "title": "Housekeeping after r-0012: refresh", "detailed_merge_status": "requested_changes"}
    assert desk.review_blocked({**mr, "detailed_merge_status": "not_approved"}) is False
    assert desk.review_blocked(mr) is True
    assert made[0][0] == "r-0012" and made[0][1] == [{"file_path": "promises/features/9-demo.yaml"}] and made[0][2]
    assert timelines["merge_requests-16"]["regenerated_from"] == 15
    assert desk.review_blocked({**mr, "iid": 16}) is True and held == [16]      # made again once; then a person


def test_card_schema_accepts_replay():
    from vallum import cards as cardlib

    card = yaml.safe_load(CARD_WITH_WATCH)
    card.update(issue=9, kind="new-endpoint", priority="P2", flag=None)
    card["promises"]["behavior"][0]["replay"] = True
    problems = [p for p in cardlib.validate(card, config.load_local("catalog.yaml")) if "replay" in p]
    assert problems == []


def test_express_fixture_has_its_own_profile():
    profile = yaml.safe_load((EXPRESS / "vallum" / "project.yaml").read_text(encoding="utf-8"))
    assert profile["language"] != "python"
    assert Path(EXPRESS / profile["source_dirs"][0]).is_dir()


# ---------------------------------------------------------------- needs-human holds and stale labels
class LabelGL:
    def __init__(self):
        self.mr_calls, self.issue_calls = [], []

    def mr_labels(self, iid, add=(), remove=()):
        self.mr_calls.append((iid, list(add), list(remove)))

    def edit_issue(self, iid, **kw):
        self.issue_calls.append((iid, kw))


def test_needs_human_hold_and_release_reset_the_session_budget():
    from vallum.desk.__main__ import Desk

    desk = Desk.__new__(Desk)
    desk.store, desk.pol, desk.notify, desk.gl = Store(MemoryState()), policy(), FakeNotify(), LabelGL()
    desk.store.set_timeline("merge_requests-5", flow_runs=3)
    mr = {"iid": 5, "title": "x", "description": "Related to #4", "labels": ["vallum"]}
    desk.hold_for_human(mr, "Pick an option", ["a", "b"], note_id=77)
    desk.hold_for_human(mr, "Pick an option", ["a", "b"], note_id=77)          # asked once, not twice
    assert len([a for a in desk.notify.alerts if a[2] == "Needs a human"]) == 1
    assert desk.gl.mr_calls[0][1] == ["vallum::needs-human"]
    held = {**mr, "labels": ["vallum", "vallum::needs-human"]}
    assert desk.human_released(held) is False                                  # label still there: still held
    assert desk.human_released(mr) is True                                     # the owner removed the label
    tl = desk.store.timeline("merge_requests-5")
    assert tl["held_for"] is None and tl["flow_runs"] == 0 and tl["released_for"] == 77


def test_release_clears_stale_hold_labels():
    """Regression: issue #4 kept vallum::needs-human after it was released and closed."""
    from vallum.drive.__main__ import Drive

    drive = Drive.__new__(Drive)
    drive.pol, drive.gl = policy(), LabelGL()
    drive.clear_labels(5, 4)
    assert "vallum::needs-human" in drive.gl.mr_calls[0][2] and "vallum::paused" in drive.gl.mr_calls[0][2]
    assert "vallum::needs-human" in drive.gl.issue_calls[0][1]["remove_labels"]


def test_labels_that_describe_a_change_are_not_scoped():
    """Regression: GitLab keeps one `vallum::` label per item, so adding vallum::ready dropped
    vallum::housekeeping from MR !6 and the Gatekeeper no longer waived rule 9."""
    pol = policy()
    for kind in ("fix", "housekeeping", "override"):
        assert "::" not in pol.label(kind), kind
    states = [pol.label(k) for k in ("ready", "blocked", "needs_human", "paused")]
    assert all(s.startswith("vallum::") for s in states)          # one state at a time is intended


def test_housekeeping_replaces_older_mrs_even_without_their_label():
    class GL:
        def __init__(self):
            self.closed, self.posted = [], []

        def open_mrs(self, label):
            return [{"iid": 6, "source_branch": "vallum/housekeeping-r-0005", "labels": ["vallum", "vallum::blocked"]},
                    {"iid": 7, "source_branch": "feature", "labels": ["vallum"]}]

        def put(self, path, json=None):
            self.closed.append(path)

        def add_note(self, kind, iid, body):
            pass

        def post(self, path, json=None):
            self.posted.append(path)
            return {"iid": 8}

    gl = GL()
    mr = housekeeping.open_mr(gl, policy(), "r-0006", [{"action": "update", "file_path": "docs/vallum/CODEMAP.md", "content": "x"}])
    assert mr["iid"] == 8 and gl.closed == ["/projects/:id/merge_requests/6"]


def test_trigger_start_fails_over_when_gitlab_drops_the_bot():
    from vallum.flows import Flows

    class GL:
        project_id = "1"

        def __init__(self, keeps):
            self.keeps, self.assignees = keeps, []

        def issue(self, iid):
            return {"assignees": [{"id": a} for a in self.assignees]}

        def set_assignees(self, kind, iid, ids):
            self.assignees = ids if self.keeps else []

    store = Store(MemoryState())
    store.b.put("vallum_flows", "release", {"service_account_id": 42})
    assert Flows(GL(keeps=True), store).start_by_trigger("release", "issues", 4, as_reviewer=False) is True
    assert Flows(GL(keeps=False), store).start_by_trigger("release", "issues", 4, as_reviewer=False) is False


def test_fix_label_counts_as_a_fix_for_the_mode_rule():
    red_core = {"core": {"mode": "red"}}
    files = ["app/features/redirect.py", "promises/features/base-redirect.yaml"]
    assert statuses(rules.evaluate(ctx(files=files, card=None, modes=red_core, is_fix=True, priority="P0")))[11] == "pass"
    assert statuses(rules.evaluate(ctx(files=files, card=None, modes=red_core, is_fix=True, priority="P2")))[11] == "fail"


def test_live_checks_wait_while_rolled_back_on_purpose():
    from vallum.patrol.__main__ import rolled_back_on_purpose

    assert rolled_back_on_purpose({"promoted_revision": "rev-9"}, "rev-7") is True
    assert rolled_back_on_purpose({"promoted_revision": "rev-9"}, "rev-9") is False
    assert rolled_back_on_purpose({}, "rev-7") is False                       # before the first Layer 3 release
    assert rolled_back_on_purpose({"promoted_revision": "rev-9"}, None) is False


class MedicGL(LabelGL):
    def __init__(self, notes=(), main="m2"):
        super().__init__()
        self._notes, self.main, self.puts = list(notes), main, []

    def notes(self, kind, iid, limit=500):
        return self._notes

    def get(self, path, **kw):
        return {"commit": {"id": self.main}}

    def put(self, path, json=None):
        self.puts.append(path)

    def mr(self, iid):
        return {"sha": "abc", "head_pipeline": {"status": "failed"}}

    def paged(self, path, **kw):
        return [{"author_name": "Vallum Incident Commander", "author_email": "service_account_x@noreply.gitlab.com"}]


def medic_desk(gl):
    from vallum.desk.__main__ import Desk

    class Flows:
        def registry(self, key):
            return {"service_account": "ai-vallum-pipeline-medic-gitlab-ai-hackathon"}

    desk = Desk.__new__(Desk)
    desk.store, desk.pol, desk.notify, desk.gl, desk.flows = Store(MemoryState()), policy(), FakeNotify(), gl, Flows()
    return desk


def test_a_failed_mr_is_rebased_again_whenever_main_moves():
    """Regression (!12): after the Medic had seen the commit, the desk never rebased it onto main's test fix."""
    gl = MedicGL()
    desk = medic_desk(gl)
    desk.store.set_timeline("merge_requests-12", medic_sha="abc", rebased_onto="m1")
    mr = {"iid": 12, "sha": "abc", "title": "x", "diverged_commits_count": 1}
    desk.send_medic(mr)
    assert gl.puts == ["/projects/:id/merge_requests/12/rebase"]
    desk.send_medic(mr)                                           # same main: no second rebase
    assert len(gl.puts) == 1


def test_a_medic_that_needs_a_human_holds_the_mr():
    note = {"id": 9, "author": {"username": "ai-vallum-pipeline-medic-gitlab-ai-hackathon"},
            "body": "🛡 Vallum: **Pipeline Medic** · attempt 1 | **Failing job:** unit-tests · **Cause:** a Vallum test · **Fix:** none: needs a human."}
    gl = MedicGL([note])
    desk = medic_desk(gl)
    desk.store.set_timeline("merge_requests-12", medic_sha="abc")
    desk.send_medic({"iid": 12, "sha": "abc", "title": "x", "diverged_commits_count": 0})
    assert any(a[2] == "Needs a human" for a in desk.notify.alerts)
    assert gl.mr_calls[0][1] == ["vallum::needs-human"]



def test_live_promises_come_from_the_live_code():
    """Regression: the merged-but-refused link preview was reported broken in production by the Live Patrol."""
    store = Store(MemoryState())
    store.set_release(last_good="vallum-6ad46fd-2981156911")
    assert store.live_commit("vallum") == "6ad46fd"
    store.set_release(promoted_commit="80b955fcd46f", promoted_revision="vallum-80b955f-1", last_good="vallum-80b955f-1")
    assert store.live_commit("vallum") == "80b955fcd46f"
    store.set_release(last_good="vallum-8fd2d63-2977660369")       # rolled back on purpose since
    assert store.live_commit("vallum") == "8fd2d63"


FIX_CARD = """feature: redirect
kind: bug-fix
tier: core
promises:
  behavior:
    - {id: old-links-work, says: Old links work, steps: ['GET /health -> 200'], where: [mr], severity: blocker}
"""
BASE_CARD = """feature: redirect
kind: new-endpoint
tier: core
promises:
  behavior:
    - {id: redirect-works, says: Redirects work, steps: ['GET /health -> 200'], where: [mr], severity: blocker}
"""


def test_two_cards_for_one_feature_both_count(tmp_path):
    """Regression (!13): a fix card with `feature: redirect` next to base-redirect.yaml hid one of the two."""
    from vallum import cards as cardlib

    d = tmp_path / "promises" / "features"
    d.mkdir(parents=True)
    (d / "6-redirect.yaml").write_text(FIX_CARD, encoding="utf-8")
    (d / "base-redirect.yaml").write_text(BASE_CARD, encoding="utf-8")
    cs = cardlib.load_from_dir(tmp_path)
    assert set(cs.cards) == {"redirect", "redirect@base-redirect"}
    assert {p.id for p in cs.promises(include_service=False)} == {"old-links-work", "redirect-works"}
    assert any(c.get("kind") == "bug-fix" for c in cs.cards.values())


def test_an_old_non_approving_verdict_asks_for_a_fresh_review():
    """Regression (!13): after a released hold the agents changed the MR, but the old needs-human verdict stayed."""
    from vallum.desk.__main__ import Desk

    class GL(LabelGL):
        def notes(self, kind, iid, limit=500):
            return [{"id": 5, "author": {"username": "ai-vallum-ship-gitlab-ai-hackathon"}, "created_at": "2026-10-25T02:26:33Z",
                     "body": '🛡 Vallum: **Reviewer verdict** · needs-human\n```json\n{"verdict": "needs-human", "sha": "old"}\n```'}]

        def mr_changes(self, iid):
            return [{"new_path": "promises/features/6-redirect.yaml", "diff": "+guard: true"}]

        def get(self, path, **kw):
            return []

        def paged(self, path, **kw):
            return [{"author_name": "Vallum Builder", "author_email": "service_account_x@noreply.gitlab.com"}]

    class Flows:
        def bots(self):
            return {"ai-vallum-ship-gitlab-ai-hackathon": "ship"}

    desk = Desk.__new__(Desk)
    desk.store, desk.pol, desk.notify, desk.gl, desk.flows = Store(MemoryState()), policy(), FakeNotify(), GL(), Flows()
    desk.store.set_timeline("merge_requests-13", released_for=5)
    started = []
    desk.start_agent = lambda mr, kind, key, fp, goal=None: started.append((kind, goal["mode"])) or True
    desk.pol.data["gate"]["agent_verdicts_required"] = True
    mr = {"iid": 13, "sha": "new", "labels": ["vallum"], "title": "x", "description": "", "created_at": "2026-10-24T17:11:00Z",
          "detailed_merge_status": "mergeable"}
    assert desk.agent_verdicts(mr) is None
    assert started == [("review", "review")]


def test_an_old_inspector_session_is_never_read_as_current():
    """Regression (!13): after the request changed, the desk re-read the previous Inspector session."""
    from vallum.desk.__main__ import Desk

    class GL(LabelGL):
        def get(self, path, **kw):
            return {"description": "the corrected request"}

    class Flows:
        def registry(self, key):
            return {"definition_sha": "new-instructions"}

    desk = Desk.__new__(Desk)
    desk.store, desk.pol, desk.notify, desk.gl, desk.flows = Store(MemoryState()), policy(), FakeNotify(), GL(), Flows()
    mr = {"iid": 13, "sha": "s1", "description": "Related to #6"}
    desk.store.set_timeline("merge_requests-13", sessions={"inspector": {"id": 1, "fp": "f", "sha": "s0"}},
                            inspector_verdict={"verdict": "gaps", "fingerprint": "f", "request_key": "old"})
    assert desk.inspector_verdict(mr, "f") is None              # neither the old verdict nor the old session counts


def test_step_zero_judges_each_promise_on_its_own_need():
    """Regression (r-0010): a popular-only watch promise was held to the route's reliability need."""
    from vallum.cards import Promise
    from vallum.drive.__main__ import Drive

    drive = Drive.__new__(Drive)
    drive.pol = policy()
    watch = Promise("w", "speed", {"measure": "p95(latency, route='GET /r/{code}', popular == 1) < 150ms"}, "x", "watch")
    rel = Promise("r", "reliability", {"measure": "error_rate(route='GET /r/{code}') < 0.1%"}, "x", "card")
    assert drive.promise_need(watch) < drive.promise_need(rel)
    assert drive.needs([watch, rel])["GET /r/{code}"] == max(2 * drive.promise_need(watch), drive.promise_need(rel))



def test_an_owner_decision_goes_to_the_builder_not_the_medic(monkeypatch):
    """Regression (!34): the owner answered a needs-human question and removed the label, but the pipeline had
    failed, so the desk sent the Pipeline Medic (which can't change promises) and nobody acted on the decision."""
    from vallum.desk.__main__ import Desk
    from vallum.desk import __main__ as deskmod

    class GL(LabelGL):
        def notes(self, kind, iid, limit=500):
            return [{"id": 77, "author": {"username": "ai-vallum-ship-gitlab-ai-hackathon"},
                     "body": '🛡 Vallum: **Reviewer verdict** · needs-human\n```json\n{"verdict": "needs-human", "sha": "abc", '
                             '"decision_needed": "404 or 422?", "options": ["404", "422"]}\n```',
                     "created_at": "2026-10-26T16:35:18Z"}]

        def mr_changes(self, iid):
            return []

    class Flows:
        def bots(self):
            return {"ai-vallum-ship-gitlab-ai-hackathon": "ship"}

    desk = Desk.__new__(Desk)
    desk.store, desk.pol, desk.notify, desk.gl, desk.flows = Store(MemoryState()), policy(), FakeNotify(), GL(), Flows()
    desk.log = lambda m: None
    started = []
    desk.start_agent = lambda mr, kind, flow, fp, goal: started.append((kind, flow, goal["mode"])) or True
    monkeypatch.setattr(deskmod.context, "agent_authors", lambda gl, iid: ["Vallum Builder"])
    mr = {"iid": 34, "title": "Link age", "description": "", "labels": ["vallum"], "sha": "abc"}
    assert desk.owner_decided(mr) is False                                     # nobody released anything yet
    desk.hold_for_human(mr, "404 or 422?", ["404", "422"], note_id=77)
    assert desk.escalated({**mr, "labels": ["vallum", "vallum::needs-human"]}) is True
    assert desk.escalated(mr) is False                                         # the owner removed the label
    assert desk.owner_decided(mr) is True and started == [("fix", "ship", "fix")]
    assert desk.owner_decided(mr) is False and len(started) == 1               # once per release


def test_the_watcher_fingerprint_has_no_array_inside_an_array():
    """Regression (!37): Firestore refused the watcher's state ("Property mrs contains an invalid nested entity")
    because the reviewers were a list inside the fingerprint list, so the desk was not re-run after the Inspector."""
    from vallum.watch import fingerprint

    fp = fingerprint({"sha": "abc", "detailed_merge_status": "not_approved", "labels": ["vallum"],
                      "reviewers": [{"id": 9}, {"id": 5}], "user_notes_count": 4}, "vallum::needs-human", "success")
    assert not any(isinstance(v, (list, tuple, dict)) for v in fp) and fp[3] == "5,9"
