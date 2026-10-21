def test_version_names_the_revision(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["revision"] == "local"
    assert "version" in r.json()["features"]
