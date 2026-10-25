"""Collect the primary facts the Gatekeeper judges: everything comes from the GitLab API or
from files produced by this pipeline, never from what an agent says."""

import json
import os
from pathlib import Path

from vallum import cards as cardlib
from vallum import config
from vallum.gate.rules import GateContext
from vallum.gitlab import GitLabError

SCAN_JOBS = {"semgrep-sast": "gl-sast-report.json", "secret_detection": "gl-secret-detection-report.json"}


def mr_changes(gl, iid: int) -> tuple[list[str], int, list[str]]:
    """Changed paths, changed line count, and the paths of files the MR adds."""
    files, lines, added = [], 0, []
    for d in gl.mr_changes(iid):
        for path in {d.get("new_path"), d.get("old_path")}:
            if path and path not in files:
                files.append(path)
        if d.get("new_file"):
            added.append(d.get("new_path"))
        lines += sum(1 for ln in (d.get("diff") or "").splitlines()
                     if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---")))
    return files, lines, added


def pipeline_facts(gl, mr: dict) -> tuple[dict | None, list | None, list[dict]]:
    """From the MR's head pipeline: the checker report, scan findings and job statuses (API, owner token)."""
    hp = mr.get("head_pipeline") or {}
    if not hp.get("id"):
        return None, None, []
    jobs = gl.pipeline_jobs(hp["id"])
    by_name = {j["name"]: j for j in jobs}
    checker = None
    job = by_name.get("promise-checker")
    if job:
        blob = gl.job_artifact(job["id"], "reports/checker.json")
        checker = json.loads(blob) if blob else None
    groups = [_vulns(gl.job_artifact(by_name[name]["id"], fname)) for name, fname in SCAN_JOBS.items() if name in by_name]
    findings = [v for g in groups if g for v in g] if any(g is not None for g in groups) else None
    return checker, findings, jobs


def author_facts(gl, author: dict) -> tuple[int | None, bool | None]:
    access, bot = None, None
    try:
        access = gl.member_access(author["id"])
    except GitLabError as exc:
        if exc.status not in (401, 403):
            raise
    try:
        bot = gl.user(author["id"]).get("bot")
    except GitLabError:
        pass
    return access, bot


def agent_authors(gl, iid: int) -> list[str]:
    """Names of Vallum agents that authored commits in this MR (service accounts of our flows)."""
    names = []
    for c in gl.paged(f"/projects/:id/merge_requests/{iid}/commits"):
        email = (c.get("author_email") or "").lower()
        name = c.get("author_name") or ""
        if "service_account" in email or name.startswith("Vallum "):
            if name not in names:
                names.append(name)
    return names


def is_housekeeping(gl, mr: dict, pol) -> bool:
    """A housekeeping MR made by code after a release: the label, no agent commits, only the code map and
    Feature Cards changed, and the cards differ from main only in their expired watch promises."""
    if pol.label("housekeeping") not in mr.get("labels", []):
        return False
    if agent_authors(gl, mr["iid"]):
        return False
    files, _lines, _added = mr_changes(gl, mr["iid"])
    allowed_doc = "docs/vallum/CODEMAP.md"
    for path in files:
        if path == allowed_doc:
            continue
        if not (path.startswith("promises/features/") and path.endswith(".yaml")):
            return False
        old = cardlib.parse_card_text(gl.file_raw_or_none(path, "main") or "")
        new = cardlib.parse_card_text(gl.file_raw_or_none(path, mr["sha"]) or "")
        old.pop("watch", None)
        new.pop("watch", None)
        if old != new:
            return False
    return True


def is_rebased(gl, sha: str) -> bool:
    main_head = gl.branch_head(os.environ.get("CI_DEFAULT_BRANCH", "main"))
    return gl.merge_base(main_head, sha) == main_head


def _vulns(blob: bytes | str | None) -> list | None:
    if blob is None:
        return None
    try:
        return json.loads(blob).get("vulnerabilities", [])
    except Exception:
        return None


def build(gl, mr: dict, mode: str, local_root: Path | None = None) -> GateContext:
    """mode='report' reads this pipeline's files from local_root; mode='desk' reads the MR's
    head pipeline artifacts through the API with the owner token."""
    pol = config.policy(gl)
    cat = config.catalog(gl)
    sha = mr["sha"]
    files, lines, added = mr_changes(gl, mr["iid"])
    access, bot = author_facts(gl, mr["author"])
    main_cards = cardlib.load_from_gitlab(gl, os.environ.get("CI_DEFAULT_BRANCH", "main"))
    branch_cards = cardlib.load_from_dir(local_root) if local_root else cardlib.load_from_gitlab(gl, sha)
    deploy_text = None
    if "deploy/service.yaml" in files:
        deploy_text = (local_root / "deploy/service.yaml").read_text(encoding="utf-8") if local_root \
            else gl.file_raw_or_none("deploy/service.yaml", sha)

    checker, findings = None, None
    pipeline_ok, pipeline_sha = None, None
    if mode == "report":
        path = (local_root or config.ROOT) / "reports" / "checker.json"
        checker = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        found = [_vulns((local_root or config.ROOT).joinpath(f).read_bytes())
                 for f in SCAN_JOBS.values() if (local_root or config.ROOT).joinpath(f).exists()]
        findings = [v for group in found if group for v in group] if found else None
    else:
        hp = mr.get("head_pipeline") or {}
        pipeline_sha = hp.get("sha")
        pipeline_ok = hp.get("status") == "success"
        checker, findings, _jobs = pipeline_facts(gl, mr)

    agents = agent_authors(gl, mr["iid"])
    labels = set(mr.get("labels", []))
    priority = next((lb.split("::", 1)[1] for lb in labels if lb.startswith("priority::")), None)
    subjects = [c for n, c in branch_cards.cards.items() if branch_cards.paths.get(n) in files]
    if not priority:
        priority = min((c.get("priority", "P2") for c in subjects), default="P2")
    is_fix = any(c.get("kind") in ("bug-fix", "security-fix") for c in subjects) or pol.label("fix") in labels

    return GateContext(
        mode=mode, policy=pol, catalog=cat, author=mr["author"], changed_files=files,
        main_cards=main_cards, branch_cards=branch_cards, sha=sha, author_access=access, author_is_bot=bot,
        changed_lines=lines, added_files=added, deploy_service_text=deploy_text, rebased=is_rebased(gl, sha),
        pipeline_ok=pipeline_ok, pipeline_sha=pipeline_sha, checker=checker, security_findings=findings,
        priority=priority, is_fix=is_fix, agent_made=bool(agents), agent_names=agents,
        learned_writer=bool(agents) and all(n == "Vallum Incident Commander" for n in agents),
    )
