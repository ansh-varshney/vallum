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
