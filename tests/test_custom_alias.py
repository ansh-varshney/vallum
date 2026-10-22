KEY = {"X-API-Key": "test-key"}
URL = "https://example.com/x"


def post(client, alias, url=URL, headers=KEY):
    return client.post("/api/aliases", json={"alias": alias, "url": url}, headers=headers)


def test_feature_is_discovered(client):
    assert "custom_alias" in set(client.app.state.features)


def test_alias_redirects(client):
    r = post(client, "promo", "https://example.com/target")
    assert r.status_code == 201
    r = client.get("/r/promo")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/target"


def test_response_shape(client):
    r = post(client, "shape-1", "https://example.com/target")
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "shape-1"
    assert body["url"] == "https://example.com/target"
    assert body["short_url"].endswith("/r/shape-1")


def test_taken_alias_is_refused(client):
    assert post(client, "taken-1", "https://example.com/a").status_code == 201
    r = post(client, "taken-1", "https://example.com/b")
    assert r.status_code == 409
    assert "alias already taken" in r.text
    # the first link is untouched
    assert client.get("/r/taken-1").headers["location"] == "https://example.com/a"


def test_alias_cannot_overwrite_a_generated_link(client):
    code = client.post("/api/links", json={"url": "https://example.com/g"}, headers=KEY).json()["code"]
    assert post(client, code).status_code == 409


def test_only_letters_digits_and_dashes(client):
    for bad in ["bad alias", "bad_alias", "bad/alias", "bad alias!", "../evil", "a.b", "", "caf\u00e9", "ok\n"]:
        assert post(client, bad).status_code == 422, bad
    assert post(client, "good-alias-1").status_code == 201
    assert post(client, "Mixed-Case-42").status_code == 201


def test_alias_length_limit(client):
    assert post(client, "a" * 64).status_code == 201
    assert post(client, "b" * 65).status_code == 422


def test_only_http_urls(client):
    for bad in ["javascript:alert(1)", "ftp://example.com", "https://a b", "x" * 10]:
        assert post(client, "test-url", bad).status_code == 422, bad


def test_url_length_limit(client):
    assert post(client, "test-long", "https://example.com/" + "x" * 2100).status_code == 422
    assert post(client, "test-ok", "https://example.com/" + "x" * 100).status_code == 201


def test_create_needs_the_key(client):
    assert client.post("/api/aliases", json={"alias": "test-1", "url": URL}).status_code == 401
    assert post(client, "test-1", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/r/test-1").status_code == 404


def test_kill_switch(client, monkeypatch):
    monkeypatch.setenv("APP_FLAGS_OFF", "custom_alias")
    assert post(client, "off-1").status_code == 404
    monkeypatch.delenv("APP_FLAGS_OFF")
    assert post(client, "off-1").status_code == 201
