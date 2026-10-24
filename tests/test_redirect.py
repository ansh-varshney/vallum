import time

KEY = {"X-API-Key": "test-key"}


def make_old_link(client, url):
    """Create a link, then age it so it predates the process start."""
    r = client.post("/api/links", json={"url": url}, headers=KEY)
    assert r.status_code == 201
    code = r.json()["code"]
    client.app.state.store._links[code]["created_at"] = "2000-01-01T00:00:00+00:00"
    return code


def test_redirect_works(client):
    code = client.post("/api/links", json={"url": "https://example.com/new"}, headers=KEY).json()["code"]
    r = client.get(f"/r/{code}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/new"


def test_older_link_redirects(client):
    code = make_old_link(client, "https://example.com/old")
    for _ in range(3):
        r = client.get(f"/r/{code}")
        assert r.status_code == 302
        assert r.headers["location"] == "https://example.com/old"


def test_unknown_code_is_404(client):
    assert client.get("/r/unknown123").status_code == 404


def test_odd_codes_are_404(client):
    assert client.get("/r/a.b").status_code == 404
    assert client.get("/r/a%27b").status_code == 404


def test_older_link_redirects_are_fast(client):
    code = make_old_link(client, "https://example.com/old-fast")
    slowest = 0.0
    for _ in range(50):
        start = time.perf_counter()
        r = client.get(f"/r/{code}")
        slowest = max(slowest, time.perf_counter() - start)
        assert r.status_code == 302
        assert r.headers["location"] == "https://example.com/old-fast"
    # the removed backfill slept 350 ms on about 1 in 5 redirects of older links
    assert slowest < 0.3, slowest
