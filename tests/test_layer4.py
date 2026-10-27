"""Layer 4: the control room (the status site), the app's pages, and what code publishes for them."""

from control import views
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


def test_control_room_pages_show_tiers_features_promises_and_events():
    data = _data()
    features = views.features_page(data["public"], {})
    assert 'fcard core broken"' in features and "2 of 3 promises kept" in features and "1 broken" in features
    assert 'href="/features/redirect"' in features and "behavior 1" in features
    card = views.feature_page(data["public"], {}, "redirect")
    assert "Broken in production" in card and "Kept in test drive r-0017 at 50%: 47 ms (limit 150 ms, n=626)" in card
    assert "Kept on its merge request" in card
    assert "A known code &lt;b&gt;redirects&lt;/b&gt;" in card and "<b>redirects</b>" not in card
    assert views.feature_page(data["public"], {}, "nope") is None
    allowance = views.allowance(data)
    assert "90%" in allowance and "2 bad of 460 visits; 20 allowed in the last 60 min" in allowance
    assert "No visits in the last 60 min" in allowance
    releases = views.releases(data, {"drive": {}, "release": {}, "project_url": ""})
    assert "Last release: r-0017 (redirect)" in releases and "!17 Housekeeping" in releases
    assert 'href="https://gitlab.example/p/-/issues/9"' in releases and "not shown" not in releases
    assert "Circuit breaker 0 of 3" in releases
    overview = views.overview(data, {"drive": {}, "release": {}, "project_url": ""}, {})
    assert "A promise is broken in production" in overview and "2<small> / 3</small>" in overview


def test_control_room_renders_with_nothing_published_and_in_safe_mode():
    empty = {name: {} for name in ("release", "modes", "recent", "queue", "meta", "public", "security", "flags")}
    nothing = {"drive": {}, "release": {}, "project_url": ""}
    assert "Every live promise is kept" in views.overview(empty, nothing, {})
    assert "publishes the live features" in views.features_page({}, {})
    assert "No releases yet" in views.releases(empty, nothing)
    assert "triages scanner findings" in views.security({}, "")
    safe = _data(meta={"safe_mode": True, "safe_mode_reason": "circuit breaker: 3 rollbacks within 6 h",
                       "rollbacks": ["2099-01-01T00:00:00Z"]},
                 release={"slot": "driving", "release_id": "r-0020", "feature": "qr", "step": 10})
    page = views.releases(safe, nothing)
    assert "Safe mode ON" in page and "circuit breaker: 3 rollbacks within 6 h" in page
    assert "Test drive r-0020 (qr): 10% of visitors on the new version." in page and "Circuit breaker 1 of 3" in page
    assert "Safe mode: nothing ships" in views.overview(safe, nothing, {})


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


FINDINGS = [
    {"id": "gid://gitlab/Vulnerability/1", "title": "Weak PRNG", "severity": "LOW", "reportType": "SAST",
     "resolvedOnDefaultBranch": True, "location": {"file": "app/features/redirect.py", "startLine": "29"}},
    {"id": "gid://gitlab/Vulnerability/2", "title": "Weak PRNG", "severity": "LOW", "reportType": "SAST",
     "resolvedOnDefaultBranch": False, "location": {"file": "simulate/visitors.py", "startLine": "82"}},
    {"id": "gid://gitlab/Vulnerability/3", "title": "SQL injection", "severity": "HIGH", "reportType": "SAST",
     "resolvedOnDefaultBranch": False, "location": {"file": "app/features/links.py", "startLine": "40"}},
    {"id": "gid://gitlab/Vulnerability/4", "title": "CVE-2026-1 in libssl3", "severity": "CRITICAL",
     "reportType": "CONTAINER_SCANNING", "resolvedOnDefaultBranch": False, "location": {"image": "registry/app:abc"}},
    {"id": "gid://gitlab/Vulnerability/5", "title": "Open redirect", "severity": "MEDIUM", "reportType": "SAST",
     "resolvedOnDefaultBranch": False, "location": {"file": "app/features/redirect.py", "startLine": "12"}},
]


def test_security_triage_decides_every_finding_and_opens_one_fix_issue_per_real_one():
    from vallum import security
    from vallum.notify import render_ledger

    class GL:
        def graphql(self, query, variables=None):
            return {"project": {"vulnerabilities": {"nodes": FINDINGS}}}

    store, opened = Store(MemoryState()), []
    profile = {"source_dirs": ["app"], "shipped": ["app"]}
    out = security.run(GL(), store, None, profile, "g/p", open_issue=lambda d: opened.append(d["id"]) or 77)
    by_id = {d["id"]: d for d in store.doc("security")["decisions"]}
    assert [by_id[i]["decision"] for i in "12345"] == ["fixed on main", "not shipped", "real", "image", "accepted"]
    assert by_id["2"]["where"] == "simulate/visitors.py:82" and "not in the app image" in by_id["2"]["reason"]
    assert opened == ["3"] and by_id["3"]["issue"] == 77 and out == ["fix issue #77 for finding 3"]
    security.run(GL(), store, None, profile, "g/p", open_issue=lambda d: opened.append(d["id"]) or 78)
    assert opened == ["3"]                                                 # never twice for the same finding
    sec = store.doc("security")
    assert sec["open"] == 5 and sec["counts"]["real"] == 1
    ledger = render_ledger({}, {}, {}, [], [], security=sec)
    assert "Security findings on main (triaged by code)" in ledger and "fix #77" in ledger
    page = views.security(sec, "")
    assert '<span class="secbig">5</span>' in page and "fix issues opened for Vallum to build" in page
    assert "simulate/visitors.py" not in page                                              # counts only
    assert "triages scanner findings on its next run" in views.security({}, "")


def test_security_fetch_pages_and_ledger_groups_image_findings():
    from vallum import security
    from vallum.notify import render_ledger

    pages = [{"project": {"vulnerabilities": {"nodes": FINDINGS[:2], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}},
             {"project": {"vulnerabilities": {"nodes": FINDINGS[2:] + [dict(FINDINGS[3], id="gid://x/6", severity="HIGH")],
                                              "pageInfo": {"hasNextPage": False}}}}]
    seen = []

    class GL:
        def graphql(self, query, variables=None):
            seen.append(variables.get("after"))
            return pages[len(seen) - 1]

    assert len(security.fetch(GL(), "g/p")) == 6 and seen == [None, "c1"]
    decisions = [security.triage(f, ["app/"]) for f in FINDINGS + [dict(FINDINGS[3], id="gid://x/6", severity="HIGH")]]
    ledger = render_ledger({}, {}, {}, [], [], security={"decisions": decisions})
    assert ledger.count("**image**") == 1 and "2 container finding(s) in the base image (1 critical, 1 high)" in ledger


PREVIEW = {"feature": "link-preview", "tier": "experimental", "flag": "link_preview", "owns": {"routes": ["GET /p/{code}"]}}
REDIRECT = {"feature": "redirect", "tier": "core", "flag": None}


class FlagsGL:
    def __init__(self, existing):
        self.flags = dict(existing)
        self.posts, self.puts = [], []

    def get(self, path, **params):
        return [{"name": n, "active": a} for n, a in self.flags.items()]

    def post(self, path, json=None):
        self.posts.append(json)
        self.flags[json["name"]] = json["active"]
        return json

    def put(self, path, json=None):
        self.puts.append((path, json))
        self.flags[path.rsplit("/", 1)[-1]] = json["active"]
        return json


def test_kill_switches_are_created_mirrored_and_flipped_only_for_non_core_features():
    from vallum import flags

    store, gl = Store(MemoryState()), FlagsGL({"version": True})
    cards = {"link-preview": PREVIEW, "redirect": REDIRECT}
    out = flags.sync(gl, store, cards)
    assert out["created"] == ["link_preview"] and gl.posts[0]["active"] is True
    assert store.doc("flags")["flags"] == {"version": True, "link_preview": True}
    gl.flags["version"] = False                                            # a member flips it in the GitLab UI
    assert flags.sync(gl, store, cards)["flipped"] == {"version": False}

    class P:
        def __init__(self, feature):
            self.feature = feature

    broken = [{"promise": P("redirect")}, {"promise": P("link-preview")}, {"promise": P("link-preview")}]
    todo = flags.to_switch_off(broken, cards, store.doc("flags")["flags"])
    assert list(todo) == ["link_preview"]                                  # Core has no switch; one flip per flag
    flags.switch(gl, store, "link_preview", False)
    assert gl.flags["link_preview"] is False and store.doc("flags")["flags"]["link_preview"] is False
    assert flags.off_features(cards, store.doc("flags")["flags"]) == {"link-preview"}
    assert flags.to_switch_off(broken, cards, store.doc("flags")["flags"]) == {}   # already off


def test_the_app_follows_the_mirror_and_a_fix_revision_sees_its_switch_on():
    from fastapi import FastAPI, HTTPException
    from starlette.requests import Request

    from app import flags as appflags

    class S:
        def __init__(self, doc):
            self.doc = doc

        def read_doc(self, collection, name):
            return self.doc

    def call(doc, revision):
        appflags._cache["at"] = 0.0
        app = FastAPI()
        app.state.store, app.state.revision = S(doc), revision
        try:
            appflags.require("link_preview")(Request({"type": "http", "app": app}))
            return 200
        except HTTPException as exc:
            return exc.status_code

    assert call({}, "r1") == 200                                           # unknown flags are on
    assert call({"flags": {"link_preview": False}}, "r1") == 404
    assert call({"flags": {"link_preview": False}, "for_revision": {"link_preview": "r2"}}, "r2") == 200
    assert call({"flags": {"link_preview": False}, "for_revision": {"link_preview": "r2"}}, "r1") == 404
    appflags._cache["at"] = 0.0


def test_the_drive_skips_switched_off_features_unless_the_release_changes_them():
    from vallum import flags
    from vallum.cards import Promise
    from vallum.drive import __main__ as drive

    store = Store(MemoryState())
    store.update(flags.DOC, lambda cur: {"flags": {"link_preview": False}})
    d = drive.Drive.__new__(drive.Drive)
    d.store, d.cards = store, CardSet(cards={"link-preview": PREVIEW, "redirect": REDIRECT})
    ps = [Promise("preview-fast", "speed", {}, "link-preview", "card"), Promise("redirect-fast", "speed", {}, "redirect", "card")]
    kept = d.switched_off(ps, changed={"redirect"}, new_rev="rev-9")
    assert [p.id for p in kept] == ["redirect-fast"] and d.skip_routes == {"GET /p/{code}"} and d.reenable == []
    assert d.without_off({"GET /p/{code}": 3.0, "GET /r/{code}": 5.0}) == {"GET /r/{code}": 5.0}
    kept = d.switched_off(ps, changed={"link-preview"}, new_rev="rev-9")
    assert len(kept) == 2 and d.reenable == ["link_preview"] and d.skip_routes == set()
    assert store.doc(flags.DOC)["for_revision"] == {"link_preview": "rev-9"}
    flags.clear_for_revision(store, "rev-9")
    assert store.doc(flags.DOC)["for_revision"] == {}


def test_every_flow_prompt_renders_the_conversation_history():
    """A resumed sub-agent session gets the Flight Director's follow-up as a new message in its history."""
    import glob

    import yaml

    for path in glob.glob(".gitlab/duo/flows/*.yaml"):
        flow = yaml.safe_load(open(path, encoding="utf-8"))
        for prompt in flow.get("prompts", []):
            assert prompt["prompt_template"].get("placeholder") == "history", (path, prompt["prompt_id"])


def test_a_housekeeping_mr_is_announced_to_the_requester():
    from vallum.drive import __main__ as drive

    notes, statuses = [], []

    class GL:
        def add_note(self, kind, iid, body):
            notes.append((kind, iid, body))

    d = drive.Drive.__new__(drive.Drive)
    d.gl, d.store = GL(), Store(MemoryState())
    d.issue_of = lambda mr: 10
    d.status = lambda mr, step, state, **kw: statuses.append((mr, step, state, kw.get("note")))
    d.announce_housekeeping({"mr_iid": 22, "release_id": "r-0024"}, {"iid": 23},
                            [{"file_path": "docs/vallum/CODEMAP.md"}])
    assert [(k, i) for k, i, _ in notes] == [("merge_requests", 22), ("issues", 10)]
    assert "opened !23" in notes[0][2] and "refreshes the code map" in notes[0][2] and "expired" not in notes[0][2]
    assert statuses == [(22, "Housekeeping", "running", "!23")]
    assert d.store.timeline("merge_requests-23")["housekeeping_for"] == {"mr": 22, "issue": 10, "release": "r-0024"}


def test_markdown_and_yaml_copies_are_escaped():
    from control.ui import markdown, yaml_html

    out = markdown("# Rules\n\nNever <script>alert(1)</script> **bold** and `code` [map](docs/x.md)\n\n"
                   "| a | b |\n|---|---|\n| <i>1</i> | 2 |\n\n- one\n- two\n\n```\n<b>raw</b>\n```", "https://g/p/-/blob/main")
    assert "<script>" not in out and "&lt;script&gt;" in out and "<b>bold</b>" in out and "<code>code</code>" in out
    assert 'href="https://g/p/-/blob/main/docs/x.md"' in out and "<table>" in out and "&lt;i&gt;1&lt;/i&gt;" in out
    assert "<ul><li>one</li><li>two</li></ul>" in out and "&lt;b&gt;raw&lt;/b&gt;" in out
    y = yaml_html('owners: [a]   # who owns the rules\nsays: "<x>"')
    assert '<span class="k">owners</span>' in y and '<span class="c"># who owns the rules</span>' in y and "&lt;x&gt;" in y


def test_rules_pages_show_the_repo_copies_read_only_one_document_each():
    docs = {"files": [{"path": "vallum/RULEBOOK.md", "title": "Rulebook", "format": "md", "text": "# Rulebook\n\n- one rule"},
                      {"path": "vallum/policy.yaml", "title": "Policy", "format": "yaml", "text": "demo: true"}],
            "commit": "abcdef1234", "at": "2026-10-26T08:00:00Z"}
    page = views.rules(docs, "https://g/p", None)
    assert "Rules and code" in page and "Read-only copy of <b>main</b> at abcdef12" in page
    assert 'href="https://g/p/-/blob/main/vallum/RULEBOOK.md"' in page and "<li>one rule</li>" in page
    assert 'href="/rules/policy"' in page and '<span class="k">demo</span>' not in page   # one document per page
    assert '<span class="k">demo</span>' in views.rules(docs, "https://g/p", "policy")
    assert views.rules(docs, "", "nope") is None
    assert "publishes a copy" in views.rules({}, "", None)


def test_app_home_and_old_status_address_and_control_room_routes(monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app import flags as appflags
    from app.main import create_app
    from app.store import MemoryStore
    from control.main import create_app as control_app

    appflags._cache["at"] = 0.0
    client = TestClient(create_app(MemoryStore()), follow_redirects=False)
    home = client.get("/")
    assert home.status_code == 200 and "Shorten a link" in home.text and "Served by version" in home.text
    assert "vallum_secrets" not in home.text and home.headers["cache-control"] == "no-store"
    assert "vallum-status-256944994567.us-central1.run.app" in home.text           # links to the control room
    moved = client.get("/status")
    assert moved.status_code == 302 and moved.headers["location"] == "https://vallum-status-256944994567.us-central1.run.app/"
    assert client.get("/status/rules").headers["location"].endswith(".run.app/rules")
    monkeypatch.setenv("APP_FLAGS_OFF", "home")
    assert client.get("/").status_code == 404                         # the home page has its own kill switch

    store = MemoryStore()
    store.write_doc("vallum_state", "drive_live", {"phase": "traffic", "pct": 10, "release_id": "r-9", "log": ["x"]})
    data = _data()
    store.write_doc("vallum_state", "public", data["public"])
    store.write_doc("vallum_state", "docs", {"files": [{"path": "vallum/policy.yaml", "title": "Policy", "format": "yaml", "text": "a: 1"}]})
    room = TestClient(control_app(store))
    assert room.get("/live.json").json()["drive"]["pct"] == 10
    overview = room.get("/")
    assert overview.status_code == 200 and "Visitors are simulated" in overview.text and '"pct": 10' in overview.text
    for path in ("/releases", "/features", "/features/redirect", "/allowance", "/security", "/rules", "/rules/policy", "/health"):
        assert room.get(path).status_code == 200, path
    assert room.get("/features/nope").status_code == 404 and room.get("/rules/nope").status_code == 404
    assert 'href="/features" class="on" aria-current=page>' in room.get("/features/redirect").text
    assert "Every live promise is kept" in overview.text or "promise is broken" in overview.text      # health in the side navigation
    for source in Path("control").glob("*.py"):
        assert "vallum_secrets" not in source.read_text(encoding="utf-8")           # the control room never reads secrets


def test_a_cold_start_at_a_step_is_warm_up_not_a_broken_promise():
    """r-0028: the new revision's first visits at 10% waited ~4 s for a cold instance; p95 over 129 visits read
    3.8 s, the release was rolled back and Core went Red. A revision's first seconds of traffic are now warm-up."""
    from vallum import allowance
    from vallum.cards import Promise
    from vallum.drive import judge

    p = Promise("redirect-fast", "speed", {"measure": "p95(latency, route='GET /r/{code}') < 150ms"}, "redirect", "card")

    def visits(rev, start, end, ms, slow_until=None):
        out, t = [], start
        while t < end:
            slow = slow_until is not None and t < slow_until
            out.append({"revision": rev, "t": t, "route": "GET /r/{code}", "ms": 3800 if slow else ms, "ok": True})
            t += 0.25
        return out

    old = visits("old", 0, 60, 55)
    cold = visits("new", 8, 60, 60, slow_until=12)          # traffic lands at 8 s, the first 4 s are a cold start
    args = dict(need=100, worse=0.2, margin=0.1, final=True)
    assert judge.judge_promise(p, old + cold, "new", "old", warmup_s=10, **args)["verdict"] == judge.KEPT
    assert judge.judge_promise(p, old + cold, "new", "old", warmup_s=0, **args)["verdict"] == judge.BROKEN  # no warm-up
    slow = visits("new", 8, 60, 3800)                       # a real slip stays slow after the warm-up
    assert judge.judge_promise(p, old + slow, "new", "old", warmup_s=10, **args)["verdict"] == judge.BROKEN
    counted = allowance.count(old + cold, {"GET /r/{code}": "core"}, {"GET /r/{code}": 150}, warmup_s=10, blame="new")
    assert counted["core"]["bad"] == 0                      # cold-start visits don't spend the allowance either


def test_every_folder_the_image_copies_is_in_the_build_context():
    import re
    from pathlib import Path

    allowed = {line.strip()[1:].rstrip("/") for line in Path(".dockerignore").read_text().splitlines() if line.startswith("!")}
    copied = re.findall(r"^COPY (\S+) ", Path("Dockerfile").read_text(), flags=re.M)
    assert copied and all(src.rstrip("/") in allowed for src in copied), (copied, allowed)   # 023c173: control/ was left out


def test_a_control_room_only_change_has_nothing_to_test_drive(monkeypatch):
    from types import SimpleNamespace

    from vallum.drive.__main__ import Drive

    class GL:
        def __init__(self, paths):
            self.paths = paths

        def compare(self, a, b):
            return {"diffs": [{"new_path": p, "old_path": p} for p in self.paths]}

    monkeypatch.setenv("CI_COMMIT_SHA", "new")
    stub = lambda paths, live="old": SimpleNamespace(store=SimpleNamespace(live_commit=lambda s: live), gl=GL(paths),
                                                    pol={"cloud_run": {"service": "vallum"}}, APP_FILES=Drive.APP_FILES)
    assert not Drive.app_changed(stub(["control/views.py", "tests/test_layer4.py"]))
    assert Drive.app_changed(stub(["control/ui.py", "app/features/home.py"]))
    assert Drive.app_changed(stub(["Dockerfile"])) and Drive.app_changed(stub([], live=None))   # unclear means yes


def test_every_sub_agent_prints_the_flight_directors_task():
    """Sessions 10034890 and 10069620: GitLab writes a new delegation into the sub-agent's `goal` (not its history),
    so a sub-agent whose prompt never prints {{ goal }} starts with no task and asks for it."""
    import yaml

    flow = yaml.safe_load(open(".gitlab/duo/flows/ship.yaml", encoding="utf-8"))
    comps = {c["name"]: c for c in flow["components"]}
    prompts = {p["prompt_id"]: p for p in flow["prompts"]}
    director = comps["flight_director"]
    assert director["model_tags"] == ["small"]                                   # the fast model coordinates
    assert "kickoff" in prompts[director["prompt_id"]]["prompt_template"]["system"]
    for sub in director["subagents"]:
        c = comps[sub["name"]]
        assert {"from": "context:goal", "as": "goal"} in c["inputs"], sub["name"]
        assert "{{ goal }}" in prompts[c["prompt_id"]]["prompt_template"]["user"], sub["name"]


def test_the_release_captain_finds_its_item_by_the_release_record():
    """Session 10074394: the goal was the bare number 16; issue #16 and MR !16 both exist, the Captain asked which
    one and left the issue open."""
    import yaml

    flow = yaml.safe_load(open(".gitlab/duo/flows/release.yaml", encoding="utf-8"))
    user = flow["prompts"][0]["prompt_template"]["user"]
    assert "Release record" in user and "issue with that number first" in user and "Never stop to ask" in user
