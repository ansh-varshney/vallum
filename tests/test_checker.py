import pytest

from vallum import cards as cardlib
from vallum import config
from vallum.cards import Promise
from vallum.checker import core, dsl
from vallum.checker.measure import compute, parse_measure, percentile, select, within


def test_parse_step_with_body_save_and_expectation():
    s = dsl.parse_step("POST /api/links {url: 'https://example.com/x-1'} -> 201 => link")
    assert (s.method, s.path, s.status, s.save) == ("POST", "/api/links", 201, "link")
    assert s.body == {"url": "https://example.com/x-1"}
    s = dsl.parse_step("GET /r/abc -> 302 https://example.com/x")
    assert s.expect == "https://example.com/x" and s.body is None
    s = dsl.parse_step("anon POST /api/links {url: 'https://e.com'} -> 401")
    assert s.anon


def test_substitute_rand_and_saved_values():
    ctx = {"rand": "r4nd", "link": {"code": "Abc1234"}}
    assert dsl.substitute("GET /r/{link.code} -> 302 https://e.com/{rand}", ctx) == "GET /r/Abc1234 -> 302 https://e.com/r4nd"
    with pytest.raises(ValueError):
        dsl.substitute("GET /r/{nope.code} -> 200", ctx)


def test_bad_step_is_reported_not_crashed(client):
    ok, detail = dsl.run_steps(client, ["FETCH /x -> 200"], "test-key")
    assert not ok and "can't read step" in detail


def test_steps_detect_a_wrong_redirect(client):
    steps = ["POST /api/links {url: 'https://example.com/{rand}'} -> 201 => link",
             "GET /r/{link.code} -> 302 https://example.com/somewhere-else"]
    ok, detail = dsl.run_steps(client, steps, "test-key")
    assert not ok and "redirects to" in detail


def test_measure_parsing_and_units():
    m = parse_measure("p95(latency, route='GET /r/{code}') < 150ms")
    assert (m.func, m.route, m.limit, m.kind) == ("p95", "GET /r/{code}", 150, "speed")
    m = parse_measure("error_rate(route='*') < 0.5%")
    assert m.limit == pytest.approx(0.005) and m.kind == "reliability"
    m = parse_measure("p95(latency, route='GET /r/{code}', link_clicks > 1000) < 0.12s # comment")
    assert m.filters == (("link_clicks", ">", 1000.0),) and m.limit == 120
    with pytest.raises(ValueError):
        parse_measure("fast enough please")


def test_measure_compute():
    samples = [{"route": "GET /r/{code}", "ms": float(i), "ok": i != 7, "revision": "a"} for i in range(1, 101)]
    m = parse_measure("p95(latency, route='GET /r/{code}') < 150ms")
    assert compute(m, select(samples, m)) == 95
    e = parse_measure("error_rate(route='*') < 0.5%")
    assert compute(e, select(samples, e)) == pytest.approx(0.01)
    assert not within(e, 0.01)
    assert select(samples, m, revision="b") == []
    assert percentile([], 95) is None


def test_static_promises(tmp_path):
    (tmp_path / "a.py").write_text("hello store", encoding="utf-8")
    p = Promise("x", "cost", {"static": {"file_lacks": ["a.py", "store"]}}, "f", "card")
    assert core.run_static_promise(p, tmp_path)["verdict"] == "broken"
    p = Promise("x", "cost", {"static": {"file_contains": ["a.py", "hello"]}}, "f", "card")
    assert core.run_static_promise(p, tmp_path)["verdict"] == "kept"


def test_every_base_mr_promise_holds_on_the_app(client):
    """The same promises the MR pipeline checks on localhost, run against the in-process app."""
    pol = config.policy()
    cs = cardlib.load_from_dir(config.ROOT)
    promises = cs.promises(where="mr")
    assert promises
    routes = {parse_measure(p.data["measure"]).route for p in promises if p.style == "measure"}
    samples = core.local_load(client, routes, 20, "test-key")
    for p in promises:
        if p.style == "steps":
            out = core.run_step_promise(client, p, "test-key", retry_s=0)
        elif p.style == "measure":
            if parse_measure(p.data["measure"]).filters:
                continue        # e.g. popular == 1: only the checker's and the test drive's traffic carries these tags
            out = core.judge_single(p, samples, 20)
        elif p.style == "static":
            out = core.run_static_promise(p, config.ROOT)
        else:
            continue
        assert out["verdict"] == "kept", (p.feature, p.id, out)
    assert pol["judge"]["mr_samples"] >= 20
