from datetime import timedelta

from simulate.visitors import plan_rates
from vallum.cards import Promise
from vallum.config import iso, utcnow
from vallum.drive import judge
from vallum.patrol.watchdog import decide, decide_lease

SPEED = Promise("redirect-fast", "speed", {"measure": "p95(latency, route='GET /r/{code}') < 150ms",
                                           "severity": "blocker", "where": ["drive"]}, "redirect", "card")


def samples(rev, ms, n, ok=True, t=10.0):
    return [{"route": "GET /r/{code}", "revision": rev, "ms": ms, "ok": ok, "t": t} for _ in range(n)]


def j(s, final=False, need=100):
    return judge.judge_promise(SPEED, s, "new", "old", need=need, worse=0.2, margin=0.05, final=final, warmup_s=5)


def test_kept_collecting_broken():
    assert j(samples("new", 60, 120) + samples("old", 60, 900))["verdict"] == judge.KEPT
    assert j(samples("new", 60, 50))["verdict"] == judge.COLLECTING
    assert j(samples("new", 380, 120) + samples("old", 90, 900))["verdict"] == judge.BROKEN


def test_warmup_visits_are_ignored():
    cold = samples("new", 3000, 30, t=1.0)
    assert j(cold + samples("new", 60, 120))["verdict"] == judge.KEPT


def test_platform_problem_when_old_is_equally_bad():
    assert j(samples("new", 200, 120) + samples("old", 190, 900))["verdict"] == judge.PLATFORM


def test_close_to_the_limit_at_the_end_is_at_risk():
    assert j(samples("new", 146, 120), final=True)["verdict"] == judge.AT_RISK
    assert j(samples("new", 146, 120), final=False)["verdict"] == judge.KEPT


def test_overall_rules():
    broken = {"verdict": judge.BROKEN, "severity": "blocker"}
    minor_broken = {"verdict": judge.BROKEN, "severity": "minor"}
    major_broken = {"verdict": judge.BROKEN, "severity": "major"}
    collecting = {"verdict": judge.COLLECTING, "severity": "blocker"}
    kept = {"verdict": judge.KEPT, "severity": "blocker"}
    assert judge.overall([broken, kept], "standard", 10, 60, False) == judge.BROKEN
    assert judge.overall([minor_broken, kept], "standard", 60, 60, True) == judge.KEPT
    assert judge.overall([major_broken], "experimental", 60, 60, True) == judge.KEPT
    assert judge.overall([major_broken], "core", 60, 60, True) == judge.BROKEN
    assert judge.overall([collecting], "standard", 70, 60, True) == judge.COLLECTING
    assert judge.overall([collecting], "standard", 121, 60, True) == judge.AT_RISK
    assert judge.overall([kept], "standard", 30, 60, False) == judge.COLLECTING
    assert judge.overall([kept], "standard", 60, 60, True) == judge.KEPT


def test_rates_reach_the_sample_on_a_small_share():
    rates = plan_rates({"GET /r/{code}": 100, "*": 200}, 0.10, 55, 20, 150, {"GET /r/{code}": 0.85, "GET /healthz": 0.15})
    assert rates["GET /r/{code}"] * 0.10 * 55 >= 100
    capped = plan_rates({"GET /r/{code}": 200}, 0.01, 115, 20, 150, {"GET /r/{code}": 1.0})
    assert sum(capped.values()) <= 150.01


def test_watchdog_decisions():
    past = iso(utcnow() - timedelta(minutes=1))
    future = iso(utcnow() + timedelta(minutes=5))
    assert decide({"slot": "idle"}) is None
    assert decide({"slot": "driving", "deadline": future}) is None
    assert decide({"slot": "driving", "deadline": past}) == "rollback"
    assert decide({"slot": "merged", "deadline": past}) == "restart_pipeline"
    assert decide({"slot": "merged", "deadline": past, "restarted": True}) == "free_slot"
    assert decide({"slot": "gated", "deadline": past}) == "free_slot"
    assert decide_lease({"state": "building", "deadline": past}) == "stall"
    assert decide_lease({"state": "done", "deadline": past}) is None
