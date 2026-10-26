KEY = {"X-API-Key": "test-key"}


def make(client, url):
    r = client.post("/api/links", json={"url": url}, headers=KEY)
    assert r.status_code == 201
    return r.json()["code"]


def test_feature_is_discovered(client):
    assert "link_exists" in set(client.app.state.features)


def test_existing_code_exists_true(client):
    code = make(client, "https://example.com/e-1")
    r = client.get(f"/api/links/{code}/exists")
    assert r.status_code == 200
    assert r.json() == {"code": code, "exists": True}


def test_missing_code_is_200_false(client):
    r = client.get("/api/links/nonexistent-1/exists")
    assert r.status_code == 200
    assert r.json() == {"code": "nonexistent-1", "exists": False}


def test_alias_exists(client):
    r = client.post("/api/aliases", json={"alias": "promo-9", "url": "https://example.com/promo"}, headers=KEY)
    assert r.status_code == 201
    assert client.get("/api/links/promo-9/exists").json() == {"code": "promo-9", "exists": True}


def test_deleted_code_no_longer_exists(client):
    code = make(client, "https://example.com/gone")
    assert client.delete(f"/api/links/{code}", headers=KEY).status_code == 204
    assert client.get(f"/api/links/{code}/exists").json() == {"code": code, "exists": False}


def test_answer_never_includes_destination(client):
    code = make(client, "https://secret.example.com/hidden-1")
    r = client.get(f"/api/links/{code}/exists")
    assert r.status_code == 200
    assert set(r.json()) == {"code", "exists"}
    for leaked in ("url", "short_url", "created_at", "secret.example.com", "hidden-1"):
        assert leaked not in r.text


def test_odd_codes_answer_false_without_touching_the_store(client, monkeypatch):
    def boom(code):
        raise AssertionError("store must not be read for an invalid code")

    monkeypatch.setattr(client.app.state.store, "get", boom)
    for bad in ["code_with_underscore", "code.with.dots", "a%40b", "caf%C3%A9", "code%00null", "code%0A"]:
        r = client.get(f"/api/links/{bad}/exists")
        assert r.status_code == 200, bad
        assert r.json()["exists"] is False, bad


def test_over_long_code_answers_false(client, monkeypatch):
    def boom(code):
        raise AssertionError("store must not be read for an invalid code")

    monkeypatch.setattr(client.app.state.store, "get", boom)
    r = client.get("/api/links/" + "a" * 65 + "/exists")
    assert r.status_code == 200
    assert r.json() == {"code": "a" * 65, "exists": False}


def test_longest_valid_code_is_looked_up(client):
    code = "a" * 64
    r = client.post("/api/aliases", json={"alias": code, "url": "https://example.com/long"}, headers=KEY)
    assert r.status_code == 201
    assert client.get(f"/api/links/{code}/exists").json() == {"code": code, "exists": True}


def test_needs_no_api_key(client):
    code = make(client, "https://example.com/pub-1")
    r = client.get(f"/api/links/{code}/exists")  # the client sends no key on GET
    assert r.status_code == 200
    assert r.json()["exists"] is True


def test_existing_link_route_is_unchanged(client):
    code = make(client, "https://example.com/same")
    r = client.get(f"/api/links/{code}")
    assert r.status_code == 200
    assert r.json()["url"] == "https://example.com/same"


def test_kill_switch(client, monkeypatch):
    code = make(client, "https://example.com/off")
    monkeypatch.setenv("APP_FLAGS_OFF", "link_exists")
    assert client.get(f"/api/links/{code}/exists").status_code == 404
    monkeypatch.delenv("APP_FLAGS_OFF")
    assert client.get(f"/api/links/{code}/exists").status_code == 200
