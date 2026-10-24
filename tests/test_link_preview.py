KEY = {"X-API-Key": "test-key"}


def make(client, url):
    r = client.post("/api/links", json={"url": url}, headers=KEY)
    assert r.status_code == 201
    return r.json()["code"]


def test_feature_is_discovered(client):
    assert "link_preview" in set(client.app.state.features)


def test_preview_shows_destination_and_continue(client):
    code = make(client, "https://example.com/page-1")
    r = client.get(f"/p/{code}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "https://example.com/page-1" in r.text
    assert "Continue" in r.text
    assert 'href="https://example.com/page-1"' in r.text


def test_preview_works_for_aliases(client):
    r = client.post("/api/aliases", json={"alias": "promo-1", "url": "https://example.com/promo"}, headers=KEY)
    assert r.status_code == 201
    r = client.get("/p/promo-1")
    assert r.status_code == 200
    assert "https://example.com/promo" in r.text


def test_unknown_code_is_404(client):
    assert client.get("/p/unknown-1").status_code == 404


def test_odd_codes_are_404(client):
    for bad in ["code@invalid", "code%00null", "code%0A", "bad_code", "a.b", "x" * 65, "caf%C3%A9"]:
        assert client.get(f"/p/{bad}").status_code == 404, bad
    assert client.get("/p/../etc/passwd").status_code == 404


def test_destination_is_html_escaped(client):
    code = make(client, "https://example.com/search?q=a&b=c")
    r = client.get(f"/p/{code}")
    assert r.status_code == 200
    assert "q=a&amp;b=c" in r.text
    assert "q=a&b=c" not in r.text


def test_markup_in_destination_is_neutralised(client):
    code = make(client, 'https://example.com/<script>alert(1)</script>"onmouseover="x')
    r = client.get(f"/p/{code}")
    assert r.status_code == 200
    assert "<script>alert" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "&quot;onmouseover=&quot;x" in r.text
    assert '"onmouseover="' not in r.text


def test_page_shows_no_secrets(client):
    code = make(client, "https://example.com/secret-1")
    r = client.get(f"/p/{code}")
    assert r.status_code == 200
    assert "https://example.com/secret-1" in r.text
    assert "test-key" not in r.text
    assert "X-API-Key" not in r.text
    assert "vallum_secrets" not in r.text


def test_preview_needs_no_key_and_does_not_write(client):
    code = make(client, "https://example.com/readonly")
    assert client.get(f"/p/{code}").status_code == 200
    assert client.get(f"/r/{code}").headers["location"] == "https://example.com/readonly"


def test_kill_switch(client, monkeypatch):
    code = make(client, "https://example.com/off")
    monkeypatch.setenv("APP_FLAGS_OFF", "link_preview")
    assert client.get(f"/p/{code}").status_code == 404
    monkeypatch.delenv("APP_FLAGS_OFF")
    assert client.get(f"/p/{code}").status_code == 200
