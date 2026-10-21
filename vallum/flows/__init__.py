"""Vallum's flows as code: publish the YAML in .gitlab/duo/flows/ to the AI Catalog, enable it in this
project, remember each flow's bot account, and start flows through the Flows API from main jobs.

Registry (state store): vallum_flows/<key> = {item_id, consumer_id, service_account, definition_sha, ...}.
Runs in the flows-sync job whenever a flow's YAML or this code changes on main.
"""

import hashlib
import json

import yaml

from vallum.config import ROOT, iso

FLOWS = {
    # key: (file, trigger types created when the flow is enabled, catalog name, catalog description)
    "ship": ("ship.yaml", ["assign_reviewer"], "Vallum Ship",
             "Turns a feature request into promises, code and a merge request, under the Rulebook."),
    "inspect": ("inspect.yaml", ["assign_reviewer"], "Vallum Inspector",
             "Independent check from an evidence bundle only: did we build what was asked, and do the facts prove it?"),
    "medic": ("medic.yaml", ["assign_reviewer"], "Vallum Pipeline Medic",
             "Reads the real failing job logs of a Vallum merge request and fixes the cause with one commit."),
    "release": ("release.yaml", ["assign_reviewer"], "Vallum Release Captain",
             "Writes plain-English release notes from the release record and closes the request issue."),
    "incident": ("incident.yaml", ["assign_reviewer"], "Vallum Incident Commander",
             "Writes the postmortem, adds a learned promise by merge request and files the fix issue."),
}
# Door A for ship (a work item moved to In progress) is created by ensure_door_a below.
# GitLab needs at least one trigger to enable a flow. Flows that only code may start (through the Flows API)
# get the member-only assign_reviewer trigger, which sync then switches off.
CODE_STARTED = {"inspect", "medic", "release", "incident"}
FLOWS_DIR = ROOT / ".gitlab" / "duo" / "flows"
REGISTRY = "vallum_flows"
WORK_ITEM_EVENT = 7   # GitLab's event type number for work item events (seen on UI-made triggers)
DOOR_A_FILTER = {"work_item": {"rules": [
    {"field": "action", "value": ["status_changed"], "operator": "in"},
    {"field": "status.name", "value": ["In progress"], "operator": "in"},
]}}


def gid(kind: str, num) -> str:
    return f"gid://gitlab/{kind}/{num}"


def num(global_id: str | None) -> int | None:
    return int(str(global_id).rsplit("/", 1)[-1]) if global_id else None


def definition(key: str) -> tuple[str, dict]:
    text = (FLOWS_DIR / FLOWS[key][0]).read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


ITEMS_Q = """query($search: String) { aiCatalogItems(search: $search, itemTypes: [FLOW]) {
  nodes { id name project { id fullPath } } } }"""
CREATE_M = """mutation($input: AiCatalogFlowCreateInput!) { aiCatalogFlowCreate(input: $input) {
  item { id name } errors } }"""
UPDATE_M = """mutation($input: AiCatalogFlowUpdateInput!) { aiCatalogFlowUpdate(input: $input) {
  item { id name } errors } }"""
CONSUMERS_Q = """query($projectId: ProjectID!, $itemId: AiCatalogItemID) {
  aiCatalogConfiguredItems(projectId: $projectId, itemId: $itemId) {
    nodes { id enabled serviceAccount { id username } item { id name }
            flowTriggers { id eventTypes description active user { id username } } } } }"""
TRIGGER_OFF_M = """mutation($input: AiFlowTriggerUpdateInput!) { aiFlowTriggerUpdate(input: $input) {
  aiFlowTrigger { id active } errors } }"""
TRIGGER_M = """mutation($input: AiFlowTriggerCreateInput!) { aiFlowTriggerCreate(input: $input) {
  aiFlowTrigger { id eventTypes filter } errors } }"""
CONSUMER_M = """mutation($input: AiCatalogItemConsumerCreateInput!) { aiCatalogItemConsumerCreate(input: $input) {
  itemConsumer { id serviceAccount { id username } } errors warnings } }"""


class Flows:
    def __init__(self, gl, store):
        self.gl = gl
        self.store = store
        self.project_gid = gid("Project", gl.project_id)

    def registry(self, key: str) -> dict:
        return self.store.b.read(REGISTRY, key) or {}

    def bots(self) -> dict:
        """username -> flow key, for checking who wrote a verdict note."""
        out = {}
        for key in FLOWS:
            sa = self.registry(key).get("service_account")
            if sa:
                out[sa] = key
        return out

    # ------------------------------------------------------------ publishing
    def find_item(self, name: str) -> str | None:
        nodes = self.gl.graphql(ITEMS_Q, {"search": name})["aiCatalogItems"]["nodes"]
        for n in nodes:
            if n["name"] == name and str(num((n.get("project") or {}).get("id"))) == str(self.gl.project_id):
                return n["id"]
        return None

    def publish(self, key: str) -> dict:
        text, spec = definition(key)
        sha = hashlib.sha256(text.encode()).hexdigest()[:16]
        reg = self.registry(key)
        name, desc = FLOWS[key][2], FLOWS[key][3]
        item_id = reg.get("item_id") or self.find_item(name)
        action = "unchanged"
        if not item_id:
            res = self.gl.graphql(CREATE_M, {"input": {"projectId": self.project_gid, "name": name, "description": desc,
                                                       "definition": text, "release": True,
                                                       "visibility": "PRIVATE"}})["aiCatalogFlowCreate"]
            if res["errors"]:
                raise RuntimeError(f"{key}: create failed: {res['errors']}")
            item_id, action = res["item"]["id"], "created"
        elif reg.get("definition_sha") != sha:
            res = self.gl.graphql(UPDATE_M, {"input": {"id": item_id, "definition": text, "description": desc,
                                                       "release": True, "versionBump": "PATCH"}})["aiCatalogFlowUpdate"]
            if res["errors"] and any("because it is released" in e for e in res["errors"]):
                action = "unchanged"  # same definition as the released version: nothing to publish
            elif res["errors"]:
                raise RuntimeError(f"{key}: update failed: {res['errors']}")
            else:
                action = "updated"
        consumer = self.ensure_enabled(key, item_id)
        if key == "ship":
            consumer = self.ensure_door_a(consumer) or consumer
        if key in CODE_STARTED:
            self.triggers_off(key, consumer)
        bot = (consumer.get("serviceAccount") or {})
        if not bot.get("username"):
            bot = next(((t.get("user") or {}) for t in consumer.get("flowTriggers") or [] if t.get("user")), {})
        reg.update(item_id=item_id, definition_sha=sha, name=name, published_at=iso(),
                   consumer_id=num(consumer["id"]), consumer_gid=consumer["id"],
                   service_account=bot.get("username"), service_account_id=num(bot.get("id")),
                   triggers=consumer.get("flowTriggers") or [])
        self.store.b.put(REGISTRY, key, reg)
        return {"key": key, "action": action, **reg}

    def ensure_enabled(self, key: str, item_id: str) -> dict:
        nodes = self.gl.graphql(CONSUMERS_Q, {"projectId": self.project_gid, "itemId": item_id})["aiCatalogConfiguredItems"]["nodes"]
        if nodes:
            return nodes[0]
        res = self.gl.graphql(CONSUMER_M, {"input": {"itemId": item_id, "target": {"projectId": self.project_gid},
                                                     "triggerTypes": FLOWS[key][1]}})["aiCatalogItemConsumerCreate"]
        if res["errors"]:
            raise RuntimeError(f"{key}: enabling failed: {res['errors']}")
        for w in res.get("warnings") or []:
            print(f"[flows] {key}: {w}")
        nodes = self.gl.graphql(CONSUMERS_Q, {"projectId": self.project_gid, "itemId": item_id})["aiCatalogConfiguredItems"]["nodes"]
        return nodes[0] if nodes else res["itemConsumer"]

    def triggers_off(self, key: str, consumer: dict):
        for t in consumer.get("flowTriggers") or []:
            if t.get("active"):
                res = self.gl.graphql(TRIGGER_OFF_M, {"input": {"id": t["id"], "active": False}})["aiFlowTriggerUpdate"]
                print(f"[flows] {key}: switched off trigger {t['id']} {res['errors'] or ''}")
                t["active"] = False

    def ensure_door_a(self, consumer: dict) -> dict | None:
        """Door A: a member moves a work item to In progress (member-only event, FR-DOOR-1)."""
        if any(WORK_ITEM_EVENT in (t.get("eventTypes") or []) for t in consumer.get("flowTriggers") or []):
            return None
        import os

        inp = {"projectPath": os.environ.get("CI_PROJECT_PATH", "gitlab-ai-hackathon/transcend-october-2026/43503198/vallum"),
               "description": "Vallum Door A: a Feature issue is moved to In progress",
               "eventTypes": [WORK_ITEM_EVENT], "aiCatalogItemConsumerId": consumer["id"], "filter": DOOR_A_FILTER}
        sa = (consumer.get("serviceAccount") or {}).get("id")
        if sa:
            inp["userId"] = sa
        res = self.gl.graphql(TRIGGER_M, {"input": inp})["aiFlowTriggerCreate"]
        if res["errors"]:
            raise RuntimeError(f"ship: Door A trigger failed: {res['errors']}")
        print(f"[flows] created Door A trigger {res['aiFlowTrigger']['id']}")
        nodes = self.gl.graphql(CONSUMERS_Q, {"projectId": self.project_gid, "itemId": consumer["item"]["id"]})
        found = nodes["aiCatalogConfiguredItems"]["nodes"]
        return found[0] if found else None

    # ------------------------------------------------------------ starting
    def start(self, key: str, goal: dict | str, issue_id: int | None = None) -> dict:
        """Start a flow with the Flows API (owner token, main jobs only). Returns {id, status, web_url}."""
        reg = self.registry(key)
        if not reg.get("consumer_id"):
            raise RuntimeError(f"flow {key} is not published yet (run the flows-sync job)")
        body = {"project_id": str(self.gl.project_id), "ai_catalog_item_consumer_id": reg["consumer_id"],
                "goal": goal if isinstance(goal, str) else json.dumps(goal, separators=(",", ":")),
                "start_workflow": True, "environment": "ambient"}
        if issue_id:
            body["issue_id"] = issue_id
        res = self.gl.post("/ai/duo_workflows/workflows", json=body)
        return {"id": res.get("id"), "status": res.get("status"), "web_url": res.get("web_url")}

    def session_status(self, workflow_id) -> str | None:
        try:
            return str(self.gl.get(f"/ai/duo_workflows/workflows/{workflow_id}").get("status") or "").lower() or None
        except Exception:
            return None

    def trace(self, workflow_id) -> str:
        return self.gl.request("GET", f"/ai/duo_workflows/workflows/{workflow_id}/trace.jsonl", raw=True).text


DONE = {"finished", "completed", "success"}
DEAD = {"failed", "stopped", "cancelled", "canceled", "error"}


def _objects_in(text: str):
    """Every top-level {...} JSON object inside a string, in order."""
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    yield json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    pass


def final_json(trace_text: str, key: str = "verdict") -> dict | None:
    """The last JSON object carrying `key` anywhere in a session trace (the agent's final answer)."""
    found = None

    def walk(node):
        nonlocal found
        if isinstance(node, dict):
            if key in node and isinstance(node.get(key), str):
                found = node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and key in node and "{" in node:
            for obj in _objects_in(node):
                walk(obj)

    for line in trace_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            walk(json.loads(line))
        except json.JSONDecodeError:
            walk(line)
    return found
