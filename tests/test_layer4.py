"""Layer 4: the public status page and what code publishes for it."""

from app.features import status
from vallum import public
from vallum.cards import CardSet
from vallum.state import MemoryState, Store

CARD = {"feature": "redirect", "tier": "core", "priority": "P1", "kind": "base", "flag": None,
        "owns": {"routes": ["GET /r/{code}"]},
        "promises": {"behavior": [{"id": "redirect-works", "says": "A known code <b>redirects</b>",
                                   "steps": ["GET /r/x -> 307"], "where": ["mr", "drive", "live"], "severity": "blocker"}],
                     "speed": [{"id": "redirect-fast", "says": "95% under 150 ms", "measure": "p95(latency) < 150ms",
                                "where": ["mr", "drive"], "severity": "blocker"}],
                     "security": [{"id": "redirect-no-secrets", "says": "No secrets", "static": {}, "where": ["mr"],
                                   "severity": "blocker"}]}}


def test_public_summary_has_promises_and_latest_results():
    store = Store(MemoryState())
    cards = CardSet(cards={"redirect": CARD})
    public.record_live(store, cards, {"redirect/redirect-works": "kept"}, "rev-1", "abc", project_url="https://x/p")
    steps = [{"step": 1, "rows": [{"feature": "redirect", "id": "redirect-fast", "verdict": "collecting", "detail": "4 of 100"}]},
             {"step": 10, "rows": [{"feature": "redirect", "id": "redirect-fast", "verdict": "kept", "detail": "52 ms"}]}]
    public.record_judged(store, "r-0001", steps)
    doc = store.doc("public")
    f = doc["features"]["redirect"]
    assert f["tier"] == "core" and [p["id"] for p in f["promises"]] == ["redirect-works", "redirect-fast", "redirect-no-secrets"]
    assert f["promises"][0]["live"] == "kept" and f["promises"][1]["live"] is None
    assert doc["judged"]["redirect/redirect-fast"] == {"release": "r-0001", "step": 10, "verdict": "kept", "detail": "52 ms"}
    assert doc["project_url"] == "https://x/p" and doc["revision"] == "rev-1"
    public.record_live(store, cards, {"redirect/redirect-works": "broken"}, "rev-2", "def")
    assert store.doc("public")["judged"]["redirect/redirect-fast"]["release"] == "r-0001"   # a patrol keeps drive results


def _data(**over):
    store = Store(MemoryState())
    public.record_live(store, CardSet(cards={"redirect": CARD}), {"redirect/redirect-works": "broken"}, "rev-1", "abc",
                       project_url="https://gitlab.example/p", ledger_url="https://gitlab.example/p/-/issues/3",
                       window_minutes=60, breaker={"limit": 3, "hours": 6})
    public.record_judged(store, "r-0017", [{"step": 50, "rows": [
        {"feature": "redirect", "id": "redirect-fast", "verdict": "kept", "detail": "47 ms (limit 150 ms, n=626)"}]}])
    data = {"release": {"slot": "idle", "release_id": "r-0017", "feature": "redirect", "released_at": "2026-10-25T05:31:21Z"},
            "modes": {"core": {"allowance": 0.9, "mode": "green", "visits": 460, "bad": 2, "allowed_bad": 20}},
            "recent": {"events": [{"at": "2026-10-25T04:58:00Z", "kind": "rolled_back", "summary": "rollback-now", "incident": 9},
                                  {"at": "2026-10-25T04:57:00Z", "kind": "housekeeping", "summary": "not shown"}]},
            "queue": {"items": ["!17 Housekeeping"]}, "meta": {"safe_mode": False, "rollbacks": []},
            "public": store.doc("public")}
    data.update(over)
    return data


def test_status_page_shows_tiers_features_promises_and_events():
    page = status.render(_data(), "vallum-fb2ef06")
    assert "Visitors are simulated" in page                        # the card's live promise
    assert "90% left" in page and "2 bad of 460 visits; 20 allowed in the last 60 min" in page and "no visits in the last 60 min" in page
    assert "Last release: r-0017 (redirect)" in page and "!17 Housekeeping" in page
    assert "broken in production" in page and "<details open>" in page    # a broken feature opens by itself
    assert "kept in test drive r-0017 at 50%: 47 ms (limit 150 ms, n=626)" in page
    assert "checked on every merge request" in page
    assert "A known code &lt;b&gt;redirects&lt;/b&gt;" in page and "<b>redirects</b>" not in page
    assert 'href="https://gitlab.example/p/-/issues/9"' in page and "not shown" not in page
    assert "Circuit breaker 0 of 3 rollbacks in 6 h" in page


def test_status_page_renders_with_nothing_published_and_in_safe_mode():
    empty = {name: {} for name in status.DOCS}
    page = status.render(empty, "local")
    assert "Visitors are simulated" in page and "publishes the live features" in page and "No releases yet" in page
    safe = status.render(_data(meta={"safe_mode": True, "safe_mode_reason": "circuit breaker: 3 rollbacks within 6 h",
                                     "rollbacks": ["2099-01-01T00:00:00Z"]},
                               release={"slot": "driving", "release_id": "r-0020", "feature": "qr", "step": 10}), "x")
    assert "Safe mode ON" in safe and "circuit breaker: 3 rollbacks within 6 h" in safe
    assert "Test drive r-0020 (qr): 10% of visitors on the new version." in safe
    assert "Circuit breaker 1 of 3" in safe


def test_container_scan_counts_only_fixable_findings_when_asked(tmp_path):
    import json

    from vallum.cards import Promise
    from vallum.checker import core

    report = {"vulnerabilities": [
        {"name": "CVE-1 in libssl3", "severity": "Critical", "solution": "Upgrade libssl3 from 3.0.11 to 3.0.15"},
        {"name": "CVE-2 in zlib", "severity": "Critical", "solution": ""},
        {"name": "CVE-3 in tar", "severity": "High", "solution": "No solution provided"}]}
    (tmp_path / "gl-container-scanning-report.json").write_text(json.dumps(report), encoding="utf-8")
    p = Promise("img", "security", {"scan": {"report": "container", "max_severity": "high", "fixable_only": True}}, "service", "service")
    out = core.run_scan_promise(p, tmp_path)
    assert out["verdict"] == "broken" and "1 fixable finding(s) above high: CVE-1 in libssl3" in out["detail"]
    report["vulnerabilities"] = report["vulnerabilities"][1:]               # only findings with no fix yet
    (tmp_path / "gl-container-scanning-report.json").write_text(json.dumps(report), encoding="utf-8")
    assert core.run_scan_promise(p, tmp_path)["verdict"] == "kept"
    assert core.run_scan_promise(p, tmp_path / "missing")["verdict"] == "skipped"


def test_step_0_judges_the_image_size_and_scan(tmp_path, monkeypatch):
    import json

    from vallum.cards import Promise
    from vallum.drive import __main__ as drive

    monkeypatch.setattr(drive, "REPORTS", tmp_path)
    (tmp_path / "build.json").write_text(json.dumps({"image_bytes": 189_000_000}), encoding="utf-8")
    small = Promise("app-image-small", "cost", {"static": {"image_below_mb": 250}, "severity": "major"}, "service", "service")
    tiny = Promise("app-image-tiny", "cost", {"static": {"image_below_mb": 100}, "severity": "major"}, "service", "service")
    scan = Promise("img", "security", {"scan": {"report": "container"}}, "service", "service")
    d = drive.Drive.__new__(drive.Drive)
    rows = {r["id"]: r for r in d.package_rows([small, tiny, scan], reports_dir=tmp_path)}
    assert rows["app-image-small"]["verdict"] == "kept"
    assert rows["app-image-tiny"]["verdict"] == "broken" and "189 MB" in rows["app-image-tiny"]["detail"]
    assert rows["img"]["verdict"] == "skipped"                             # no scan report in this pipeline
