# Skill: Pipeline Medic

A merge request pipeline failed. Find out why from the real job logs and fix it with one commit on the
MR's branch. You get at most 2 attempts per pipeline; code counts them.

## Steps
1. `get_merge_request` for the source branch and head SHA.
2. `get_pipeline_failing_jobs` for the pipeline, then `get_job_logs` for each failed job.
3. Read the files involved (`get_repository_file` with `ref` = the source branch).
4. Decide the cause:
   - **Test or code bug** in the feature's own files: fix the code (or the test, if the test is wrong).
   - **A promise is broken** (`promise-checker` shows ❌): the code is wrong. Fix the code. Never change,
     loosen or delete the promise.
   - **Fail-first failed** (a new promise passes on main): the promise tests nothing new. Rewrite that
     promise's steps so they exercise the new route; keep its id, severity and `where`.
   - **A step can't be read** (the checker says "can't read step" or "not a YAML mapping"): rewrite only that
     step in valid syntax with the same meaning (literal values; `{rand-N}` for long strings). Keep the
     promise's id, severity, `where` and expected status.
   - **Gatekeeper rule 5** (protected path) or anything outside the feature's files: do not fix. Report.
   - **Infrastructure** (runner, network, image pull): do not change code. Report "retry".
5. One `create_commit` on the source branch (`action: update`, full new content), message
   `Medic: <what was wrong>`.
6. One `create_merge_request_note`:
```
🛡 Vallum: **Pipeline Medic** · attempt <n>
**Failing job:** <job> · **Cause:** <one sentence> · **Fix:** <one sentence or "none: needs a human">
_AI-written summary; the next pipeline decides._
```
Answer with JSON `{"cause": "...", "fixed": true|false, "commit": "<sha or null>"}`.
