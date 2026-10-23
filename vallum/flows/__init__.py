"""Vallum's flows as code: publish the YAML in .gitlab/duo/flows/ to the AI Catalog, enable it in this
project, remember each flow's bot account, and start flows through the Flows API from main jobs.

Registry (state store): vallum_flows/<key> = {item_id, consumer_id, service_account, definition_sha, ...}.
Runs in the flows-sync job whenever a flow's YAML or this code changes on main.
"""

import hashlib
import json
import re

import yaml

from vallum.config import ROOT, iso

MENTION, ASSIGN, ASSIGN_REVIEWER, PIPELINE, WORK_ITEM = 0, 1, 2, 3, 7   # GitLab's trigger event numbers
EVENT_NAMES = {MENTION: "mention", ASSIGN: "assign", ASSIGN_REVIEWER: "assign_reviewer", WORK_ITEM: "work_item"}
DOOR_A_FILTER = {"work_item": {"rules": [
    {"field": "action", "value": ["status_changed"], "operator": "in"},
    {"field": "status.name", "value": ["In progress"], "operator": "in"},
]}}

# Every trigger is a member-only event, except mentions (the Cockpit), whose commands code only carries out
# for members. Code on main starts agents by taking the same member-only actions with the owner's account
# (assigning a flow's bot as reviewer or assignee), so they run on the flow's service account and bill the
# namespace pool. Only the Inspector is started through the Flows API, because its input is the bundle.
FLOWS = {
    "ship": {"file": "ship.yaml", "name": "Vallum Ship",
             "description": "Turns a feature request into promises, code and a merge request, under the Rulebook.",
             "triggers": {ASSIGN_REVIEWER: {"active": True, "why": "Door B, and code asking for a review or a fix"},
                          WORK_ITEM: {"active": True, "filter": DOOR_A_FILTER,
                                      "why": "Vallum Door A: a Feature issue is moved to In progress"}}},
    "inspect": {"file": "inspect.yaml", "name": "Vallum Inspector",
                "description": "Independent check from an evidence bundle only: did we build what was asked, and do the facts prove it?",
                "triggers": {ASSIGN_REVIEWER: {"active": False, "why": "enabling needs one; started by code with the bundle"}}},
    "medic": {"file": "medic.yaml", "name": "Vallum Pipeline Medic",
              "description": "Reads the real failing job logs of a Vallum merge request and fixes the cause with one commit.",
              "triggers": {ASSIGN_REVIEWER: {"active": True, "why": "code assigns the Medic to a failed merge request"}}},
    "release": {"file": "release.yaml", "name": "Vallum Release Captain",
                "description": "Writes plain-English release notes from the release record and closes the request issue.",
                "triggers": {ASSIGN: {"active": True, "why": "code assigns the Captain to the released request"},
                             ASSIGN_REVIEWER: {"active": False, "why": "created when enabled; not used"}}},
    "incident": {"file": "incident.yaml", "name": "Vallum Incident Commander",
                 "description": "Writes the postmortem, adds a learned promise by merge request and files the fix issue.",
                 "triggers": {ASSIGN: {"active": True, "why": "code assigns the Commander to the incident issue"},
                              ASSIGN_REVIEWER: {"active": False, "why": "created when enabled; not used"}}},
    "cockpit": {"file": "cockpit.yaml", "name": "Vallum Cockpit",
                "description": "Answers questions and explains; turns requests into exact @vallum commands that code carries out.",
                "triggers": {MENTION: {"active": True, "why": "someone mentions the Cockpit bot"}}},
}
FLOWS_DIR = ROOT / ".gitlab" / "duo" / "flows"
REGISTRY = "vallum_flows"


def gid(kind: str, num) -> str:
    return f"gid://gitlab/{kind}/{num}"


def num(global_id: str | None) -> int | None:
    return int(str(global_id).rsplit("/", 1)[-1]) if global_id else None


def definition(key: str) -> tuple[str, dict]:
    text = (FLOWS_DIR / FLOWS[key]["file"]).read_text(encoding="utf-8")
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
        name, desc = FLOWS[key]["name"], FLOWS[key]["description"]
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
        consumer = self.ensure_triggers(key, consumer)
        bot = (consumer.get("serviceAccount") or {})
        if not bot.get("username"):
            bot = next(((t.get("user") or {}) for t in consumer.get("flowTriggers") or [] if t.get("user")), {})
        reg.update(item_id=item_id, definition_sha=sha, name=name, published_at=iso(),
                   consumer_id=num(consumer["id"]), consumer_gid=consumer["id"],
                   service_account=bot.get("username"), service_account_id=num(bot.get("id")),
                   triggers=consumer.get("flowTriggers") or [])
        self.store.b.put(REGISTRY, key, reg)
        return {"key": key, "action": action, **reg}

    def consumer(self, item_id: str) -> dict | None:
        nodes = self.gl.graphql(CONSUMERS_Q, {"projectId": self.project_gid, "itemId": item_id})["aiCatalogConfiguredItems"]["nodes"]
        return nodes[0] if nodes else None

    def ensure_enabled(self, key: str, item_id: str) -> dict:
        found = self.consumer(item_id)
        if found:
            return found
        first = next(iter(FLOWS[key]["triggers"]))
        res = self.gl.graphql(CONSUMER_M, {"input": {"itemId": item_id, "target": {"projectId": self.project_gid},
                                                     "triggerTypes": [EVENT_NAMES[first]]}})["aiCatalogItemConsumerCreate"]
        if res["errors"]:
            raise RuntimeError(f"{key}: enabling failed: {res['errors']}")
        for w in res.get("warnings") or []:
            print(f"[flows] {key}: {w}")
        return self.consumer(item_id) or res["itemConsumer"]

    def ensure_triggers(self, key: str, consumer: dict) -> dict:
        """Make the flow's triggers exactly what FLOWS says: create missing ones, switch the rest on or off."""
        import os

        wanted = FLOWS[key]["triggers"]
        existing = consumer.get("flowTriggers") or []
        changed = False
        for event, spec in wanted.items():
            match = next((t for t in existing if event in (t.get("eventTypes") or [])), None)
            if match is None and spec["active"]:
                inp = {"projectPath": os.environ.get("CI_PROJECT_PATH", "gitlab-ai-hackathon/transcend-october-2026/43503198/vallum"),
                       "description": f"{FLOWS[key]['name']}: {spec['why']}", "eventTypes": [event],
                       "aiCatalogItemConsumerId": consumer["id"]}
                if spec.get("filter"):
                    inp["filter"] = spec["filter"]
                sa = (consumer.get("serviceAccount") or {}).get("id")
                if sa:
                    inp["userId"] = sa
                res = self.gl.graphql(TRIGGER_M, {"input": inp})["aiFlowTriggerCreate"]
                if res["errors"]:
                    raise RuntimeError(f"{key}: trigger {EVENT_NAMES[event]} failed: {res['errors']}")
                print(f"[flows] {key}: created {EVENT_NAMES[event]} trigger")
                changed = True
            elif match is not None and bool(match.get("active")) != spec["active"]:
                self.set_trigger(key, match, spec["active"])
                changed = True
        for t in existing:
            if t.get("active") and not any(e in wanted for e in (t.get("eventTypes") or [])):
                self.set_trigger(key, t, False)
                changed = True
        return (self.consumer(consumer["item"]["id"]) or consumer) if changed else consumer

    def set_trigger(self, key: str, trigger: dict, active: bool):
        res = self.gl.graphql(TRIGGER_OFF_M, {"input": {"id": trigger["id"], "active": active}})["aiFlowTriggerUpdate"]
        print(f"[flows] {key}: trigger {trigger['id']} {'on' if active else 'off'} {res['errors'] or ''}")
        trigger["active"] = active

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

    def start_by_trigger(self, key: str, kind: str, iid: int, as_reviewer: bool) -> bool:
        """Start a flow the way a member would: assign its bot as reviewer (MRs) or assignee (issues or MRs).
        The flow runs on its own service account and bills the namespace pool, not anyone's personal credits.
        The bot is removed first, so adding it again is a fresh event."""
        bot = self.registry(key).get("service_account_id")
        if not bot:
            return False
        item = self.gl.mr(iid) if kind == "merge_requests" else self.gl.issue(iid)
        field = "reviewers" if as_reviewer else "assignees"
        others = [u["id"] for u in item.get(field) or [] if u.get("id") != bot]
        if as_reviewer:
            self.gl.set_reviewers(iid, others)
            self.gl.set_reviewers(iid, others + [bot])
        else:
            self.gl.set_assignees(kind, iid, others)
            self.gl.set_assignees(kind, iid, others + [bot])
        # GitLab silently drops users it can't assign: only report success if the bot really is there now
        after = self.gl.mr(iid) if kind == "merge_requests" else self.gl.issue(iid)
        return any(u.get("id") == bot for u in after.get(field) or [])

    def sessions_on(self, kind: str, iid: int, key: str, since: str) -> list[dict]:
        """Sessions a flow's bot reported on an issue or MR ("started session [id] triggered by @user")."""
        bot = self.registry(key).get("service_account")
        out = []
        for n in self.gl.notes(kind, iid):
            if (n.get("author") or {}).get("username") != bot or (n.get("created_at") or "") < since:
                continue
            m = re.search(r"started session \[(\d+)\].*?triggered by @([\w.-]+)", n.get("body") or "")
            if m:
                out.append({"id": int(m.group(1)), "by": m.group(2), "at": n.get("created_at")})
        return out

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
