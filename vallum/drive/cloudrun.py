"""Cloud Run through its Knative-style REST API (the same calls `gcloud run services replace` makes).

Settings come from deploy/service.yaml (FR-TUNE-1); this module only fills in the image, the
revision name and the traffic split."""

import copy
import time

import yaml

from vallum.config import ROOT


class DeployFailed(Exception):
    pass


def expected_split(traffic: list[dict]) -> dict:
    """revision -> percent that a traffic spec asks for (entries with 0% only carry a tag)."""
    out = {}
    for t in traffic:
        if t.get("percent"):
            out[t["revisionName"]] = out.get(t["revisionName"], 0) + int(t["percent"])
    return out


class CloudRun:
    def __init__(self, session, project: str, region: str, service: str):
        self.s = session
        self.project = project
        self.region = region
        self.service = service
        self.base = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project}/services"

    def _check(self, resp, what: str):
        if resp.status_code >= 300:
            raise DeployFailed(f"{what}: HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.content else {}

    def get(self) -> dict | None:
        resp = self.s.get(f"{self.base}/{self.service}")
        if resp.status_code == 404:
            return None
        return self._check(resp, "read service")

    def _put(self, body: dict) -> dict:
        body = copy.deepcopy(body)
        body.pop("status", None)
        body["metadata"]["namespace"] = self.project
        if self.get() is None:
            body["metadata"].pop("resourceVersion", None)
            return self._check(self.s.post(self.base, json=body), "create service")
        return self._check(self.s.put(f"{self.base}/{self.service}", json=body), "replace service")

    def wait_ready(self, timeout: float = 300) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            svc = self.get()
            meta, status = svc.get("metadata", {}), svc.get("status", {})
            if status.get("observedGeneration", 0) >= meta.get("generation", 1):
                ready = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
                if ready.get("status") == "True":
                    return svc
                if ready.get("status") == "False":
                    raise DeployFailed(f"{ready.get('reason')}: {ready.get('message')}")
            if time.monotonic() > deadline:
                raise DeployFailed("service did not become ready in time")
            time.sleep(3)

    def deploy(self, image: str, revision: str, traffic: list[dict], env: dict | None = None) -> dict:
        """Create a new revision from deploy/service.yaml with the given traffic split."""
        spec = yaml.safe_load((ROOT / "deploy" / "service.yaml").read_text(encoding="utf-8"))
        current = self.get()
        body = copy.deepcopy(current) if current else {"apiVersion": spec["apiVersion"], "kind": spec["kind"],
                                                         "metadata": {"name": self.service}, "spec": {}}
        body["metadata"].setdefault("annotations", {}).update(spec["metadata"].get("annotations", {}))
        template = copy.deepcopy(spec["spec"]["template"])
        template.setdefault("metadata", {})["name"] = revision
        container = template["spec"]["containers"][0]
        container["image"] = image
        container["env"] = [{"name": k, "value": str(v)} for k, v in (env or {}).items()]
        body["spec"]["template"] = template
        body["spec"]["traffic"] = traffic
        self._put(body)
        return self.wait_ready()

    def set_traffic(self, traffic: list[dict], confirm_s: float = 0) -> dict:
        """Change the split. Cloud Run applies it within seconds but reports it (status.traffic, Ready) only after
        one to several minutes; waiting for the report made r-0011's rollback crash. Every visit names the revision
        that served it, so judging doesn't need the report; a rollback measures the switch on the live app."""
        svc = self.get()
        svc["spec"]["traffic"] = traffic
        out = self._put(svc)
        if confirm_s:
            try:
                return self.wait_traffic(expected_split(traffic), timeout=confirm_s)
            except DeployFailed:
                return self.get() or out
        return out

    def wait_traffic(self, expected: dict, timeout: float = 120, poll_s: float = 1.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            svc = self.get()
            meta, status = svc.get("metadata", {}), svc.get("status", {})
            if status.get("observedGeneration", 0) >= meta.get("generation", 1) and self.serving(svc) == expected:
                return svc
            ready = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
            if ready.get("status") == "False" and status.get("observedGeneration", 0) >= meta.get("generation", 1):
                raise DeployFailed(f"{ready.get('reason')}: {ready.get('message')}")
            if time.monotonic() > deadline:
                raise DeployFailed(f"traffic did not reach {expected} in time")
            time.sleep(poll_s)

    def ensure_public(self):
        url = f"https://run.googleapis.com/v1/projects/{self.project}/locations/{self.region}/services/{self.service}"
        policy = self._check(self.s.get(f"{url}:getIamPolicy"), "read IAM policy")
        for b in policy.get("bindings", []):
            if b.get("role") == "roles/run.invoker" and "allUsers" in b.get("members", []):
                return
        policy.setdefault("bindings", []).append({"role": "roles/run.invoker", "members": ["allUsers"]})
        self._check(self.s.post(f"{url}:setIamPolicy", json={"policy": policy}), "make the service public")

    @staticmethod
    def serving(svc: dict | None) -> dict:
        """revision -> percent currently serving."""
        out = {}
        for t in (svc or {}).get("status", {}).get("traffic", []) or []:
            if t.get("percent"):
                out[t["revisionName"]] = out.get(t["revisionName"], 0) + int(t["percent"])
        return out

    @staticmethod
    def tag_url(svc: dict, tag: str) -> str | None:
        for t in svc.get("status", {}).get("traffic", []) or []:
            if t.get("tag") == tag:
                return t.get("url")
        return None

    @staticmethod
    def main_revision(svc: dict | None) -> str | None:
        serving = CloudRun.serving(svc)
        return max(serving, key=serving.get) if serving else None
