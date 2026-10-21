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


def mr_changes(gl, iid: int) -> tuple[list[str], int, str | None]:
    files, lines, deploy_text = [], 0, None
    for d in gl.mr_changes(iid):
        for path in {d.get("new_path"), d.get("old_path")}:
            if path and path not in files:
                files.append(path)
        lines += sum(1 for ln in (d.get("diff") or "").splitlines()
                     if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---")))
    return files, lines, deploy_text


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
    files, lines, _ = mr_changes(gl, mr["iid"])
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
        if hp.get("id"):
            jobs = {j["name"]: j for j in gl.pipeline_jobs(hp["id"])}
            job = jobs.get("promise-checker")
            if job:
                blob = gl.job_artifact(job["id"], "reports/checker.json")
                checker = json.loads(blob) if blob else None
            groups = []
            for name, fname in SCAN_JOBS.items():
                if name in jobs:
                    groups.append(_vulns(gl.job_artifact(jobs[name]["id"], fname)))
            findings = [v for g in groups if g for v in g] if any(g is not None for g in groups) else None

    labels = set(mr.get("labels", []))
    priority = next((lb.split("::", 1)[1] for lb in labels if lb.startswith("priority::")), None)
    subjects = [c for n, c in branch_cards.cards.items() if branch_cards.paths.get(n) in files]
    if not priority:
        priority = min((c.get("priority", "P2") for c in subjects), default="P2")
    is_fix = any(c.get("kind") in ("bug-fix", "security-fix") for c in subjects)

    return GateContext(
        mode=mode, policy=pol, catalog=cat, author=mr["author"], changed_files=files,
        main_cards=main_cards, branch_cards=branch_cards, sha=sha, author_access=access, author_is_bot=bot,
        changed_lines=lines, deploy_service_text=deploy_text, rebased=is_rebased(gl, sha),
        pipeline_ok=pipeline_ok, pipeline_sha=pipeline_sha, checker=checker, security_findings=findings,
        priority=priority, is_fix=is_fix,
    )
