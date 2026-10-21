# Vallum Day-1 checks

Temporary kit that proves the GitLab and Google Cloud behaviour Vallum's design relies on.
Removed when Phase 1 starts.

| # | Check | How |
|---|---|---|
| 1 | A trigger can start a flow from repo YAML | Trigger "work item created" → `.gitlab/duo/flows/hello.yaml`, then open an issue |
| 2 | The flow knows who started it and can look up membership | Read the hello comment and the session log |
| 3 | GitLab Duo reviews MRs opened by a flow's service account | Mention trigger → `hello-mr.yaml`, then watch the MR |
| 4 | An owner-token merge on main starts a pipeline-event trigger | Run `check:merge`, watch for a hello comment on issue #1 |
| 5 | The Flows API starts a catalog flow | Run `check:flows-api` with `HELLO_CONSUMER_ID` |
| 6 | Shortest pipeline-schedule interval | Pipeline schedule `*/5 * * * *` on main, check `check:scheduled` runs |
| 7 | We can create a pipeline execution policy | Secure → Policies → New policy (look only) |
| 8 | Keyless Cloud Run deploy, tag URL, 90/10 split | Run `check:cloud-run` |
| 9 | Firestore emulator inside CI | Run `check:emulator` |
| 10 | Owner token only on main | `check:token-visibility` on main vs. any other branch |
| 11 | Credits used | Avatar → GitLab Credits after checks 1–5 |
