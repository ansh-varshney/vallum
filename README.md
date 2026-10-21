# 🛡 Vallum

**A hands-off release guardian on GitLab Duo Agent Platform.** Vallum ships a requested feature to
production by itself, but only if the feature keeps its promises, and only as boldly as its mistake
allowance permits. Agents write code, promises and explanations; **code decides what ships**.

Built for the GitLab Transcend "Life After Code" hackathon (Path A, Hands-off).
Live app: <https://vallum-256944994567.us-central1.run.app/status>

> Status: **Layer 1, the safe skeleton**, is in place: promise checker, Gatekeeper, Merge Desk,
> deploy provenance gate, test drive on Cloud Run, state store, Notifier, Watchdog and visitor simulation.
> The agents (Layer 2) come next.

## How a change ships today (Layer 1)

1. A project member opens a merge request with a Feature Card in `promises/features/` and adds the label `vallum`.
2. The **MR pipeline** (no tokens, no cloud access) runs the unit tests, the **promise checker** on a localhost copy
   of the app with the Firestore emulator (including the *fail-first* check: new behavior promises must fail on
   `main` and pass on the branch), SAST, secret detection and the **Gatekeeper report**.
3. The **Merge Desk** (a job on `main`, `resource_group: release`, every 5 minutes and at the end of every main
   pipeline) picks the next ready MR by priority, rebases it, waits for a fresh pipeline on that exact commit,
   runs all **12 Gatekeeper rules**, records the commit as sanctioned, gives the owner's approval that the MR
   approval rule needs, and merges pinned to that SHA.
4. The **main pipeline** starts with the **deploy provenance gate** (refuses commits that skipped the Merge Desk),
   builds the image into Artifact Registry and runs the **test drive** on Cloud Run: deploy with 0% traffic, step 0
   on the private address, then the tier's traffic steps (Standard: 10% → 50% → 100%) while simulated visitors
   play the users. Kept means promote; broken or unclear means **instant rollback**.
5. The **Notifier** keeps one live status comment on the MR, alerts the change owner, and mirrors the state
   store into the **Ledger** issue. The **Watchdog** undoes anything left half-done.

Emergency: run the manual job **`rollback-now`** in any `main` pipeline.

## Repository

| Path | What it is |
|---|---|
| `app/` | FastAPI link shortener; one module per feature in `app/features/` |
| `promises/` | Feature Cards, app-wide promises (`service.yaml`), learned promises |
| `vallum/` 🔒 | Rulebook, policy, catalog, card schema and all of Vallum's code |
| `vallum/checker/` | Promise checker (steps, measure, static, scan) and fail-first check |
| `vallum/gate/` | Gatekeeper (12 rules), provenance gate |
| `vallum/desk/` | Merge Desk |
| `vallum/drive/` | Test-drive controller (Cloud Run REST API, keyless login) |
| `vallum/patrol/` | Live Patrol and Watchdog |
| `vallum/notify/` | Status comments, alerts, Ledger |
| `simulate/` | Visitor simulation run by CI jobs |
| `deploy/service.yaml` | Cloud Run settings as code |

🔒 = protected path: the Gatekeeper refuses changes from anyone but the owner.

## Google Cloud

Cloud Run (revisions, tags, traffic split, min instances 0), Artifact Registry, Firestore (app data and the
state store), and Workload Identity Federation from protected `main` pipelines only. No key files exist.
**Visitors are simulated** by CI jobs during the hackathon; nothing runs on Google Cloud between drives.

## Run it locally

```bash
pip install -r requirements.txt -r requirements-vallum.txt
pytest
APP_STORE=memory APP_API_KEY=dev uvicorn app.main:app --reload
```

## License

MIT
