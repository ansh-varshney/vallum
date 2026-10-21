"""A small GitLab REST client. With no token it reads public data only (MR pipelines);
on main it uses the owner token from the protected, masked OWNER_TOKEN variable."""

import os
import time
from urllib.parse import quote

import httpx


class GitLabError(Exception):
    def __init__(self, status, method, path, body):
        super().__init__(f"GitLab {method} {path} -> {status}: {body[:300]}")
        self.status = status
        self.body = body


class GitLab:
    def __init__(self, token: str | None = None, project_id: str | None = None, api_url: str | None = None):
        self.api = (api_url or os.environ.get("CI_API_V4_URL") or "https://gitlab.com/api/v4").rstrip("/")
        self.project_id = str(project_id or os.environ.get("CI_PROJECT_ID") or "87894346")
        self.token = token
        headers = {"User-Agent": "vallum"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        self.http = httpx.Client(headers=headers, timeout=httpx.Timeout(60, connect=15))

    @classmethod
    def from_env(cls, require_token: bool = False) -> "GitLab":
        token = os.environ.get("OWNER_TOKEN") or None
        if require_token and not token:
            raise SystemExit("OWNER_TOKEN is not available here (it only exists in pipelines on main).")
        return cls(token=token)

    @property
    def can_write(self) -> bool:
        return bool(self.token)

    # --- low level -------------------------------------------------------
    def _url(self, path: str) -> str:
        if path.startswith("/projects/:id"):
            path = f"/projects/{self.project_id}" + path[len("/projects/:id"):]
        return self.api + path

    def request(self, method: str, path: str, *, params=None, json=None, data=None, ok=(200, 201, 202, 204), raw=False):
        attempts = 5
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                resp = self.http.request(method, self._url(path), params=params, json=json, data=data)
            except httpx.TransportError:
                # timeouts and dropped connections: GitLab's API is sometimes slow; try again
                if last:
                    raise
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and not last:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if resp.status_code not in ok:
            raise GitLabError(resp.status_code, method, path, resp.text)
        if raw:
            return resp
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.request("PUT", path, json=json, **kw)

    def paged(self, endpoint, limit=500, **params):
        params = {"per_page": 100, **params}
        page = 1
        out = []
        while len(out) < limit:
            params["page"] = page
            resp = self.request("GET", endpoint, params=params, raw=True)
            items = resp.json()
            out.extend(items)
            nxt = resp.headers.get("x-next-page")
            if not nxt:
                break
            page = int(nxt)
        return out[:limit]

    # --- repository ------------------------------------------------------
    def file_raw(self, path: str, ref: str = "main") -> str:
        resp = self.request("GET", f"/projects/:id/repository/files/{quote(path, safe='')}/raw", params={"ref": ref}, raw=True)
        return resp.text

    def file_raw_or_none(self, path: str, ref: str = "main") -> str | None:
        try:
            return self.file_raw(path, ref)
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def tree(self, path: str, ref: str = "main") -> list[dict]:
        try:
            return self.paged("/projects/:id/repository/tree", path=path, ref=ref)
        except GitLabError as exc:
            if exc.status == 404:
                return []
            raise

    def commit(self, sha: str) -> dict:
        return self.get(f"/projects/:id/repository/commits/{sha}")

    def compare(self, base: str, head: str) -> dict:
        return self.get("/projects/:id/repository/compare", **{"from": base, "to": head, "straight": "true"})

    def merge_base(self, a: str, b: str) -> str:
        resp = self.request("GET", "/projects/:id/repository/merge_base", params=[("refs[]", a), ("refs[]", b)])
        return resp["id"]

    def branch_head(self, branch: str = "main") -> str:
        return self.get(f"/projects/:id/repository/branches/{quote(branch, safe='')}")["commit"]["id"]

    def commit_mrs(self, sha: str) -> list[dict]:
        return self.get(f"/projects/:id/repository/commits/{sha}/merge_requests") or []

    # --- people -----------------------------------------------------------
    def member_access(self, user_id: int) -> int:
        """Effective access level (inherited too), or 0 for non-members."""
        try:
            return int(self.get(f"/projects/:id/members/all/{user_id}")["access_level"])
        except GitLabError as exc:
            if exc.status == 404:
                return 0
            raise

    def user(self, user_id: int) -> dict:
        return self.get(f"/users/{user_id}")

    # --- merge requests ---------------------------------------------------
    def mr(self, iid: int) -> dict:
        return self.get(f"/projects/:id/merge_requests/{iid}", include_diverged_commits_count="true", include_rebase_in_progress="true")

    def mr_changes(self, iid: int) -> list[dict]:
        return self.paged(f"/projects/:id/merge_requests/{iid}/diffs")

    def open_mrs(self, label: str) -> list[dict]:
        return self.paged("/projects/:id/merge_requests", state="opened", target_branch="main", labels=label)

    def mr_labels(self, iid: int, add: list[str] = (), remove: list[str] = ()):
        payload = {}
        if add:
            payload["add_labels"] = ",".join(add)
        if remove:
            payload["remove_labels"] = ",".join(remove)
        if payload:
            return self.put(f"/projects/:id/merge_requests/{iid}", json=payload)

    def pipeline_jobs(self, pipeline_id: int) -> list[dict]:
        return self.paged(f"/projects/:id/pipelines/{pipeline_id}/jobs", include_retried="false")

    def job_artifact(self, job_id: int, path: str) -> bytes | None:
        try:
            return self.request("GET", f"/projects/:id/jobs/{job_id}/artifacts/{path}", raw=True).content
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    # --- notes and issues --------------------------------------------------
    def notes(self, kind: str, iid: int) -> list[dict]:
        return self.paged(f"/projects/:id/{kind}/{iid}/notes", sort="asc", order_by="created_at")

    def add_note(self, kind: str, iid: int, body: str) -> dict:
        return self.post(f"/projects/:id/{kind}/{iid}/notes", json={"body": body})

    def edit_note(self, kind: str, iid: int, note_id: int, body: str) -> dict:
        return self.put(f"/projects/:id/{kind}/{iid}/notes/{note_id}", json={"body": body})

    def create_issue(self, title: str, description: str, labels: list[str] = ()) -> dict:
        return self.post("/projects/:id/issues", json={"title": title, "description": description, "labels": ",".join(labels)})

    def edit_issue(self, iid: int, **fields) -> dict:
        return self.put(f"/projects/:id/issues/{iid}", json=fields)
