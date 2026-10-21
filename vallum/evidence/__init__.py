"""The evidence bundle (B15): the Inspector's only input, built by code from primary sources.

Contains the request text, the merge request diff, the Feature Card(s) at the MR head, the head pipeline's
job statuses, the promise checker report and scan findings. It never contains comments, agent summaries,
decision logs or session content (FR-INSP-2)."""

import hashlib
import json
import re

import yaml

MAX_DIFF = 6000
MAX_TOTAL = 40000
_RELATED = re.compile(r"(?:related to|closes|fixes|resolves|implements)\s+#(\d+)", re.I)


def related_issue(description: str | None) -> int | None:
    m = _RELATED.search(description or "")
    return int(m.group(1)) if m else None


def done_when_lines(text: str | None) -> list[str]:
    """Bullets under the Feature template's "How will we know it works?" heading."""
    lines, inside = [], False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            inside = "know it works" in line.lower() or "done when" in line.lower()
            continue
        if inside and line.startswith(("-", "*")):
            item = line.lstrip("-* ").strip()
            if item and not item.startswith("[ ]"):
                lines.append(item)
    return lines


def fingerprint(diffs: list[dict]) -> str:
    """Stable across rebases that don't change the MR's own changes."""
    h = hashlib.sha256()
    for d in sorted(diffs, key=lambda d: d.get("new_path") or ""):
        h.update((d.get("new_path") or "").encode())
        body = "\n".join(ln for ln in (d.get("diff") or "").splitlines() if not ln.startswith("@@"))
        h.update(body.encode())
    return h.hexdigest()[:20]


def build(gl, mr: dict, checker: dict | None, jobs: list[dict], findings: list | None) -> dict:
    diffs = gl.mr_changes(mr["iid"])
    issue_iid = related_issue(mr.get("description"))
    request = {"source": f"merge request !{mr['iid']} description", "text": mr.get("description") or ""}
    if issue_iid:
        try:
            issue = gl.get(f"/projects/:id/issues/{issue_iid}")
            request = {"source": f"issue #{issue_iid}", "title": issue.get("title"), "text": issue.get("description") or ""}
        except Exception:
            pass
    request["done_when"] = done_when_lines(request.get("text"))

    cards = []
    for d in diffs:
        path = d.get("new_path") or ""
        if path.startswith("promises/features/") and path.endswith(".yaml") and not d.get("deleted_file"):
            text = gl.file_raw_or_none(path, mr["sha"])
            if text:
                try:
                    cards.append({"path": path, "card": yaml.safe_load(text)})
                except yaml.YAMLError:
                    cards.append({"path": path, "card": None, "error": "not valid YAML"})

    total = 0
    diff_out = []
    for d in diffs:
        body = d.get("diff") or ""
        if len(body) > MAX_DIFF:
            body = body[:MAX_DIFF] + "\n... (truncated by code)"
        total += len(body)
        if total > MAX_TOTAL:
            body = "(omitted: bundle size limit)"
        diff_out.append({"path": d.get("new_path"), "new_file": d.get("new_file"), "deleted_file": d.get("deleted_file"),
                         "diff": body})

    checker_view = None
    if checker:
        checker_view = {
            "sha": checker.get("sha"), "ok": checker.get("ok"),
            "promises": [{k: r.get(k) for k in ("feature", "id", "type", "severity", "verdict", "detail")}
                         for r in checker.get("promises", [])],
            "fail_first": checker.get("fail_first"),
        }
    severities = {}
    for v in findings or []:
        s = str(v.get("severity", "unknown")).lower()
        severities[s] = severities.get(s, 0) + 1

    return {
        "kind": "vallum-evidence-bundle/v1",
        "merge_request_iid": mr["iid"],
        "sha": mr["sha"],
        "fingerprint": fingerprint(diffs),
        "title": mr.get("title"),
        "request": request,
        "diff": diff_out,
        "cards": cards,
        "pipeline": {"id": (mr.get("head_pipeline") or {}).get("id"),
                     "jobs": [{"name": j["name"], "status": j["status"], "allow_failure": j.get("allow_failure", False)}
                              for j in jobs]},
        "checker": checker_view,
        "security_findings": severities if findings is not None else "no scan results",
    }


def as_goal(bundle: dict) -> str:
    return json.dumps(bundle, separators=(",", ":"), default=str)
