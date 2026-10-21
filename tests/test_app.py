KEY = {"X-API-Key": "test-key"}


def test_features_are_discovered(client):
    assert {"redirect", "links", "health", "status"} <= set(client.app.state.features)


def test_create_redirect_read_delete(client):
    r = client.post("/api/links", json={"url": "https://example.com/a"}, headers=KEY)
    assert r.status_code == 201
    code = r.json()["code"]
    assert len(code) == 7

    r = client.get(f"/r/{code}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/a"

    r = client.get(f"/api/links/{code}")
    assert r.status_code == 200 and r.json()["url"] == "https://example.com/a"

    assert client.delete(f"/api/links/{code}", headers=KEY).status_code == 204
    assert client.get(f"/r/{code}").status_code == 404
    assert client.delete(f"/api/links/{code}", headers=KEY).status_code == 404


def test_write_needs_the_key(client):
    assert client.post("/api/links", json={"url": "https://example.com"}).status_code == 401
    assert client.post("/api/links", json={"url": "https://example.com"}, headers={"X-API-Key": "nope"}).status_code == 401


def test_only_http_urls(client):
    for bad in ["javascript:alert(1)", "ftp://x", "https://a b", "x" * 10]:
        assert client.post("/api/links", json={"url": bad}, headers=KEY).status_code == 422


def test_odd_codes_are_refused(client):
    assert client.get("/r/a.b").status_code == 404
    assert client.get("/r/unknown123").status_code == 404


def test_every_response_names_revision_and_route(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.headers["x-revision"] == "local"
    assert r.headers["x-route"] == "GET /health"
    assert client.get("/r/zzz").headers["x-route"] == "GET /r/{code}"


def test_status_page(client):
    r = client.get("/status")
    assert r.status_code == 200
    assert "Visitors are simulated" in r.text
