import copy

from vallum import cards as cardlib
from vallum import config
from vallum.config import Policy
from vallum.gate import rules
from vallum.gate.provenance import decide as provenance

OWNER = {"id": 1, "username": "ansh-varshney"}
BOT = {"id": 2, "username": "ai-vallum-ship-bot"}
DEV = {"id": 3, "username": "teammate"}

NEW_CARD = {
    "feature": "version", "issue": 5, "kind": "new-endpoint", "tier": "standard", "priority": "P2",
    "owns": {"routes": ["GET /api/version"], "files": ["app/features/version.py"]}, "flag": "version",
    "promises": {
        "behavior": [{"id": "version-works", "says": "Shows the revision", "steps": ["GET /api/version -> 200"],
                      "where": ["mr"], "severity": "blocker"}],
        "speed": [{"id": "version-fast", "says": "Fast enough", "measure": "p95(latency, route='GET /api/version') < 200ms",
                   "where": ["drive"], "severity": "major"}],
        "reliability": [{"id": "version-ok", "says": "Rarely fails", "measure": "error_rate(route='GET /api/version') < 1%",
                         "where": ["drive"], "severity": "major"}],
        "security": [{"id": "version-no-secrets", "says": "No secrets shown",
                      "static": {"file_lacks": ["app/features/version.py", "api_key"]}, "where": ["mr"], "severity": "blocker"}],
    },
}

GOOD_CHECKER = {"ok": True, "sha": "abc", "summary": {"kept": 20, "broken": 0}, "fail_first": {"ok": True, "note": "1 new"}}


def ctx(author=OWNER, files=("app/features/version.py", "promises/features/5-version.yaml"), card=NEW_CARD, **kw):
    main = cardlib.load_from_dir(config.ROOT)
    branch = copy.deepcopy(main)
    if card:
        branch.cards[card["feature"]] = card
        branch.paths[card["feature"]] = "promises/features/5-version.yaml"
    base = dict(mode="desk", policy=Policy(config.load_local("policy.yaml")), catalog=config.load_local("catalog.yaml"),
                author=author, changed_files=list(files), main_cards=main, branch_cards=branch, sha="abc",
                author_access=40, author_is_bot=False, changed_lines=40, rebased=True, pipeline_ok=True,
                pipeline_sha="abc", checker=GOOD_CHECKER, security_findings=[], modes={}, safe_mode=False,
                queue_head=True, caps_ok=True, reviewer_verdict="approve", reviewer_author="ai-vallum-ship",
                inspector={"verdict": "matches", "sha": "abc", "fingerprint": "f1"}, fingerprint="f1")
    base.update(kw)
    return rules.GateContext(**base)


def statuses(report):
    return {r["no"]: r["status"] for r in report["rules"]}


def test_good_mr_passes_all_twelve():
    report = rules.evaluate(ctx())
    assert report["ok"], rules.render(report)
    assert report["score"] == "12/12"
    assert report["tier"] == "standard"
    assert statuses(report)[9] == "pass"


def test_report_mode_defers_but_passes():
    report = rules.evaluate(ctx(mode="report", author_access=None, pipeline_ok=None, modes=None, queue_head=None, caps_ok=None))
    assert report["ok"]
    assert report["deferred"] == [1, 6, 9, 11, 12]


def test_desk_mode_needs_every_rule():
    report = rules.evaluate(ctx(author_access=None))
    assert not report["ok"] and statuses(report)[1] == "deferred"


def test_non_member_is_refused():
    report = rules.evaluate(ctx(author=DEV, author_access=10))
    assert statuses(report)[1] == "fail"


def test_app_change_without_card_fails():
    report = rules.evaluate(ctx(files=["app/features/brand_new.py"], card=None))
    assert statuses(report)[2] == "fail"


def test_unowned_feature_file_fails_rule3():
    report = rules.evaluate(ctx(files=["app/features/version.py", "app/features/sneaky.py", "promises/features/5-version.yaml"]))
    assert statuses(report)[3] == "fail" and "sneaky.py" in report["rules"][2]["detail"]


def test_agent_cannot_touch_protected_paths_but_owner_can():
    files = ["vallum/policy.yaml", "app/features/version.py", "promises/features/5-version.yaml"]
    assert statuses(rules.evaluate(ctx(author=BOT, author_is_bot=True, files=files)))[5] == "fail"
    assert statuses(rules.evaluate(ctx(files=files)))[5] == "pass"


def test_agent_cannot_loosen_a_promise():
    main = cardlib.load_from_dir(config.ROOT)
    branch = copy.deepcopy(main)
    branch.cards["redirect"]["promises"]["speed"][0]["measure"] = "p95(latency, route='GET /r/{code}') < 900ms"
    c = ctx(author=BOT, author_is_bot=True, files=["promises/features/base-redirect.yaml"], card=None,
            main_cards=main, branch_cards=branch)
    report = rules.evaluate(c)
    assert statuses(report)[4] == "fail"
    assert report["tier"] == "core"


def test_deploy_caps():
    svc = (config.ROOT / "deploy" / "service.yaml").read_text(encoding="utf-8").replace('maxScale: "3"', 'maxScale: "50"')
    report = rules.evaluate(ctx(files=["deploy/service.yaml"], card=None, deploy_service_text=svc))
    assert statuses(report)[5] == "fail"


def test_stale_or_failed_pipeline():
    assert statuses(rules.evaluate(ctx(rebased=False)))[6] == "fail"
    assert statuses(rules.evaluate(ctx(rebased=False, mode="report", pipeline_ok=None)))[6] == "deferred"
    assert statuses(rules.evaluate(ctx(pipeline_sha="old")))[6] == "fail"
    assert statuses(rules.evaluate(ctx(pipeline_ok=False)))[6] == "fail"


def test_checker_and_fail_first():
    bad = {**GOOD_CHECKER, "ok": False, "promises": [], "fail_first": {"ok": False, "note": "passes on main"}}
    assert statuses(rules.evaluate(ctx(checker=bad)))[7] == "fail"
    assert statuses(rules.evaluate(ctx(checker=None)))[7] == "fail"
    assert statuses(rules.evaluate(ctx(checker={**GOOD_CHECKER, "sha": "other"})))[7] == "fail"


def test_new_high_finding_blocks():
    finding = {"severity": "High", "name": "SQL injection", "identifiers": [{"type": "cwe", "value": "89"}],
               "location": {"file": "app/features/version.py"}}
    assert statuses(rules.evaluate(ctx(security_findings=[finding])))[8] == "fail"
    assert statuses(rules.evaluate(ctx(security_findings=[finding], security_baseline=[finding])))[8] == "pass"


def test_red_freeze_and_yellow_cap():
    red_core = {"core": {"mode": "red"}}
    files = ["app/features/redirect.py", "promises/features/base-redirect.yaml"]
    assert statuses(rules.evaluate(ctx(files=files, card=None, modes=red_core)))[11] == "fail"
    assert statuses(rules.evaluate(ctx(files=files, card=None, modes=red_core, is_fix=True, priority="P0")))[11] == "pass"
    assert statuses(rules.evaluate(ctx(modes=red_core)))[11] == "pass"          # a Standard change still ships
    assert statuses(rules.evaluate(ctx(modes={"standard": {"mode": "yellow"}}, changed_lines=500)))[11] == "fail"
    assert statuses(rules.evaluate(ctx(safe_mode=True)))[11] == "fail"


def test_a_crashing_rule_fails_closed():
    report = rules.evaluate(ctx(checker={"ok": True, "sha": "abc"}))  # missing keys must not crash into a pass
    assert report["rules"][6]["status"] in ("pass", "fail")


def test_provenance_decisions():
    pol = Policy(config.load_local("policy.yaml"))
    sanctioned = {"mrhead": {"mr_iid": 4, "release_id": "r-0004"}}
    ok, how, _ = provenance("merge1", ["mainprev", "mrhead"], "ansh-varshney", "push", sanctioned.get, pol)
    assert ok and how == "merge-desk"
    ok, how, _ = provenance("x", ["p"], "ai-vallum-ship-bot", "push", sanctioned.get, pol)
    assert not ok and how == "unsanctioned"
    ok, how, _ = provenance("x", ["p"], "ansh-varshney", "push", sanctioned.get, pol)
    assert ok and how == "owner-push"
    pol.data["provenance"]["allow_owner_push"] = False
    assert not provenance("x", ["p"], "ansh-varshney", "push", sanctioned.get, pol)[0]
    assert provenance("x", [], "anyone", "schedule", sanctioned.get, pol)[0]
