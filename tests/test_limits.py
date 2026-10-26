EXPECTED = {"max_url_length": 2048, "max_code_length": 64}


def test_feature_is_discovered(client):
    assert "limits" in set(client.app.state.features)


def test_limits_returns_constants(client):
    r = client.get("/api/limits")
    assert r.status_code == 200
    assert r.json() == EXPECTED


def test_needs_no_api_key(client):
    r = client.get("/api/limits")  # the client sends no key on GET
    assert r.status_code == 200
    assert r.json() == EXPECTED


def test_wrong_api_key_is_ignored(client):
    r = client.get("/api/limits", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 200
    assert r.json() == EXPECTED


def test_post_is_not_allowed(client):
    r = client.post("/api/limits", json={"data": "test"})
    assert r.status_code == 405


def test_extra_path_is_404(client):
    assert client.get("/api/limits/extra").status_code == 404


def test_query_parameters_are_ignored(client):
    r = client.get("/api/limits?param=value")
    assert r.status_code == 200
    assert r.json() == EXPECTED


def test_limits_match_what_the_api_enforces(client):
    limits = client.get("/api/limits").json()
    key = {"X-API-Key": "test-key"}
    prefix = "https://example.com/"
    ok_url = prefix + "a" * (limits["max_url_length"] - len(prefix))
    assert client.post("/api/links", json={"url": ok_url}, headers=key).status_code == 201
    too_long = ok_url + "a"
    assert client.post("/api/links", json={"url": too_long}, headers=key).status_code == 422


def test_kill_switch(client, monkeypatch):
    monkeypatch.setenv("APP_FLAGS_OFF", "limits")
    assert client.get("/api/limits").status_code == 404
    monkeypatch.delenv("APP_FLAGS_OFF")
    assert client.get("/api/limits").status_code == 200
