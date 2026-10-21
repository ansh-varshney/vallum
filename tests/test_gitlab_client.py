import httpx

from vallum.gitlab import GitLab


def test_tree_passes_path_as_a_query_parameter():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"type": "blob", "path": "promises/features/a.yaml"}])

    gl = GitLab(project_id="1", api_url="https://gitlab.example/api/v4")
    gl.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert gl.tree("promises/features", ref="main")[0]["path"].endswith("a.yaml")
    assert "path=promises%2Ffeatures" in seen["url"] and "ref=main" in seen["url"]


def test_timeouts_are_retried(monkeypatch):
    monkeypatch.setattr("vallum.gitlab.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, text="feature: x")

    gl = GitLab(project_id="1", api_url="https://gitlab.example/api/v4")
    gl.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert gl.file_raw("promises/features/x.yaml", ref="abc") == "feature: x"
    assert calls["n"] == 3
