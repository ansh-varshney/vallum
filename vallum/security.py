"""Security triage (Secure stage): every open scanner finding on main gets a decision made by code, with a
structured reason, on every Live Patrol run (zero credits).

    fixed on main   no longer detected on main
    not shipped     the file is not in the app image (the profile's `shipped` dirs): CI tooling, simulators
    real            high or critical in shipped code: a P1 `vallum-fix` issue, which Door C builds (B1)
    accepted        low or medium in shipped code: tracked, does not block (rule 8 blocks new high/critical)
    image           container findings: judged at step 0 instead (app-image-no-fixable-critical)

At Developer role GitLab does not let Vallum change a finding's status (adminVulnerability is false), so
decisions are recorded on the Ledger and the status page; a Maintainer applies dismissals."""

from vallum.config import iso

DOC = "security"
FIXED, NOT_SHIPPED, REAL, ACCEPTED, IMAGE = "fixed on main", "not shipped", "real", "accepted", "image"

QUERY = """query($path: ID!, $after: String) { project(fullPath: $path) {
  vulnerabilities(first: 100, after: $after, state: [DETECTED, CONFIRMED]) { pageInfo { hasNextPage endCursor }
  nodes { id title severity reportType resolvedOnDefaultBranch webUrl
    location { ... on VulnerabilityLocationSast { file startLine }
               ... on VulnerabilityLocationSecretDetection { file startLine }
               ... on VulnerabilityLocationDependencyScanning { file }
               ... on VulnerabilityLocationContainerScanning { image } } } } } }"""


def shipped_prefixes(profile: dict) -> list[str]:
    return [d.rstrip("/") + "/" for d in (profile.get("shipped") or profile.get("source_dirs") or [])]


def triage(finding: dict, shipped: list[str]) -> dict:
    loc = finding.get("location") or {}
    file = loc.get("file") or ""
    sev = str(finding.get("severity") or "unknown").lower()
    report = str(finding.get("reportType") or "").lower()
    where = f"{file}:{loc['startLine']}" if file and loc.get("startLine") else (file or loc.get("image") or "")
    base = {"id": str(finding.get("id", "")).rsplit("/", 1)[-1], "title": finding.get("title") or "?",
            "severity": sev, "report": report, "where": where, "url": finding.get("webUrl") or ""}
    if finding.get("resolvedOnDefaultBranch"):
        return {**base, "decision": FIXED, "reason": "no longer detected on main; a Maintainer can mark it resolved"}
    if report == "container_scanning" or loc.get("image"):
        return {**base, "decision": IMAGE, "reason": "judged at step 0: a fixable critical refuses the release"}
    if file and not any(file.startswith(p) for p in shipped):
        return {**base, "decision": NOT_SHIPPED,
                "reason": f"{file} is not in the app image (it ships {', '.join(shipped)}); dismissal reason: not applicable"}
    if sev in ("critical", "high"):
        return {**base, "decision": REAL, "reason": "high or critical in shipped code: a fix issue is opened and built (Door C)"}
    return {**base, "decision": ACCEPTED, "reason": f"{sev} severity in shipped code: tracked; rule 8 blocks new high or critical"}


def fetch(gl, project_path: str, max_pages: int = 10) -> list[dict]:
    nodes, after = [], None
    for _ in range(max_pages):
        page = (gl.graphql(QUERY, {"path": project_path, "after": after}).get("project") or {}).get("vulnerabilities") or {}
        nodes += page.get("nodes") or []
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return nodes


def summary(decisions: list[dict]) -> dict:
    counts = {}
    for d in decisions:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    return {"open": len(decisions), "counts": counts, "at": iso()}


def run(gl, store, pol, profile: dict, project_path: str, open_issue=None) -> list[str]:
    """Triage every open finding; open one fix issue per real finding (once). Returns what changed."""
    decisions = [triage(f, shipped_prefixes(profile)) for f in fetch(gl, project_path)]
    state = store.doc(DOC)
    issues = dict(state.get("issues") or {})
    out = []
    for d in decisions:
        if d["decision"] == REAL and d["id"] not in issues and open_issue:
            issues[d["id"]] = open_issue(d)
            out.append(f"fix issue #{issues[d['id']]} for finding {d['id']}")
        if d["id"] in issues:
            d["issue"] = issues[d["id"]]
    store.update(DOC, lambda cur: {**cur, "decisions": decisions, "issues": issues, **summary(decisions)})
    return out or [f"{len(decisions)} open finding(s) triaged"]
