"""Deploy provenance gate (FR-SEC-4): the first job of every main pipeline.

At our role, main lets Developers (and so agent bots) push. This job refuses to build or deploy a
commit that didn't come through the Merge Desk: the merged MR's head commit must be recorded in
the state store before the merge. During the build phase the owner's own pushes are allowed and
recorded as overrides (policy: provenance.allow_owner_push).

    python -m vallum.gate.provenance
"""

import os
import sys
from pathlib import Path

from vallum import config
from vallum.gitlab import GitLab
from vallum.notify import Notifier
from vallum.state import Store

DEPLOYING = ("push", "api")   # main pipelines that build and deploy (api: the Watchdog's restart)


def decide(sha: str, parents: list[str], pusher: str, source: str, lookup, pol) -> tuple[bool, str, dict]:
    if source not in DEPLOYING:
        return True, "not-a-push", {"detail": f"{source} pipeline: nothing is deployed from it"}
    for candidate in [sha] + list(parents[1:]):
        record = lookup(candidate)
        if record:
            return True, "merge-desk", {**record, "detail": f"Merge Desk merge of !{record.get('mr_iid')}"}
    if pusher in pol.owners and pol.get("provenance", {}).get("allow_owner_push"):
        return True, "owner-push", {"detail": f"pushed by the owner @{pusher} (allowed during the build phase, recorded)"}
    return False, "unsanctioned", {"detail": f"@{pusher} pushed {sha[:8]} to main without the Merge Desk"}


def open_revert(gl, sha: str) -> str | None:
    branch = f"vallum/revert-{sha[:8]}"
    try:
        gl.post("/projects/:id/repository/branches", json={"branch": branch, "ref": "main"})
        gl.post(f"/projects/:id/repository/commits/{sha}/revert", json={"branch": branch})
        mr = gl.post("/projects/:id/merge_requests", json={
            "source_branch": branch, "target_branch": "main", "remove_source_branch": True,
            "title": f"Revert {sha[:8]}: pushed to main without the Merge Desk",
            "description": f"🛡 Vallum: the deploy provenance gate stopped commit {sha} before it was built or deployed. "
                           "Merge this to undo it, or close it if the change was intended.",
        })
        return mr["web_url"]
    except Exception as exc:
        print(f"[provenance] could not open a revert MR: {exc}")
        return None


def main():
    pol = config.policy()
    sha = os.environ["CI_COMMIT_SHA"]
    source = os.environ.get("CI_PIPELINE_SOURCE", "push")
    pusher = os.environ.get("GITLAB_USER_LOGIN", "")
    if source not in DEPLOYING:
        ok, how, info = decide(sha, [], pusher, source, lambda _s: None, pol)
        print(f"Provenance: {how} · {info['detail']}")
        return
    gl = GitLab.from_env(require_token=True)
    store = Store.from_env()
    parents = gl.commit(sha).get("parent_ids", [])
    ok, how, info = decide(sha, parents, pusher, source, store.sanctioned, pol)
    out = Path("reports/provenance.env")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"VALLUM_PROVENANCE={how}\nVALLUM_RELEASE_ID={info.get('release_id', '')}\nVALLUM_MR_IID={info.get('mr_iid', '')}\n",
        encoding="utf-8")
    print(f"Provenance: {how} · {info['detail']}")
    notifier = Notifier(gl, store, pol)
    if ok:
        if how == "owner-push":
            store.event("override", f"owner push {sha[:8]} deployed without the Merge Desk", sha=sha, by=pusher)
        return
    revert = open_revert(gl, sha)
    store.event("provenance", f"stopped unsanctioned commit {sha[:8]} by @{pusher}", sha=sha, by=pusher)
    notifier.alert(
        "issues", None, "Unsanctioned push stopped", author=pol.owners[0] if pol.owners else None,
        what=f"Commit `{sha[:8]}` reached main without going through the Merge Desk (pushed by @{pusher}).",
        impact="None in production: the pipeline stopped before building or deploying it.",
        done=f"Stopped this pipeline. {'Opened a revert MR: ' + revert if revert else 'Could not open a revert MR automatically.'}",
        nxt="Production stays on the last good revision. The next Merge Desk release will include this commit unless it is reverted.",
        action="Yes: merge the revert MR, or tell Vallum the change was intended by merging it through an MR.",
    )
    notifier.ledger()
    sys.exit(1)


if __name__ == "__main__":
    main()
