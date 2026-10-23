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

    def notes(self, kind, iid):
        return self.items.get((kind, iid), [])

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
    shifted = re.sub(r" :(\d+)", lambda m: f" :{int(m.group(1)) + 3}", text)   # only line numbers moved
    shifted = re.sub(r", (\d+) lines\)", lambda m: f", {int(m.group(1)) + 1} lines)", shifted)
    assert housekeeping.changes(ROOT, [], {"docs/vallum/CODEMAP.md": shifted}.get) == []
    assert housekeeping.changes(ROOT, [], {}.get)[0]["action"] == "create"


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
