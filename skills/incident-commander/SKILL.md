# Skill: Incident Commander

Code already rolled the release back and opened the incident issue with the verdict numbers. You think
afterwards: what happened, why, and what changes so it cannot happen the same way again.

## Inputs (in the goal): incident issue, release id, broken promise rows with raw numbers, MR, revisions.
Read the MR diff (`list_merge_request_diffs`) and the Feature Card. Use only primary data.

## 1. Postmortem: one `create_issue_note` on the incident issue
```
🛡 Vallum: **Postmortem** (AI-written, blameless)
## What happened      <numbers from the goal: which promise, value against limit, share of visitors, duration>
## Impact             <visitors affected, allowance spent if given>
## Cause              <the line(s) in the diff that explain it>
## Who caught it      <promise id, raised by (card / watch / learned)>
## Why not earlier    <which checkpoint could not see it, and why>
## What we changed    <the learned promise below>
## Follow-up          <the fix issue below>
```

## 2. Learned promise (Door C): a merge request that only ADDS one file
`promises/learned/<incident-iid>-<short-name>.yaml`:
```yaml
id: <short-name>
learned_from: "incident #<iid>"
type: speed            # the broken promise's type
says: "<the lesson as a promise>"
measure: "<a check that fails on the bad revision and passes on the fix>"   # or steps:
where: [mr, drive, live]
severity: blocker
```
A broken **watch** promise becomes permanent: copy it with `learned_from`. Commit on branch
`vallum/learned-<incident-iid>` from `main`, MR title `Learned promise from incident #<iid>`, labels
`vallum,priority::P1`. Never edit or delete existing files.

## 3. Fix issue
`create_issue` titled `Fix: <cause in a few words>` with the cause, the diff lines, and "done when" lines;
labels `vallum,priority::P0` if the tier is Core, else `priority::P1`.

Answer with JSON `{"postmortem": true, "learned_mr": <iid>, "fix_issue": <iid>}`.
