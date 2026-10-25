# Skill: Incident Commander

Code already rolled the release back and opened the incident issue; you were assigned to it. Its description
holds the incident record (JSON with the broken promises and raw numbers). You think afterwards: what happened,
why, and what changes so it cannot happen the same way again.

## Read
`docs/vallum/GUIDELINES.md`, the incident issue (`get_issue`), the MR it names (`get_merge_request`,
`list_merge_request_diffs`) and the Feature Card. Use only primary data.

## 1. Postmortem: one `create_issue_note` on the incident issue
```
🛡 Vallum: **Postmortem** (AI-written, blameless)
## What happened      <numbers from the record: which promise, value against limit, share of users, duration>
## Impact             <users affected, allowance spent if given>
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
replay: true           # only if step 0 must replay real traffic to catch it (for example busy items)
where: [mr, drive, live]
severity: blocker
```
A broken **watch** promise becomes permanent: copy it with `learned_from`. Commit on branch
`vallum/learned-<incident-iid>` from `main`, MR title `Learned promise from incident #<iid>`, labels
`vallum,priority::P1`. Never edit or delete existing files.

## 3. Fix issue (Door C: code starts the Ship flow on it by itself)
Every "How will we know it works?" line must be provable by a promise (a card's, the learned one, or one the
fix adds); never list outcomes like modes or allowances recovering, which code measures by itself.
`create_issue` titled `Fix: <cause in a few words>` with the cause, the diff lines, and a "How will we know it
works?" section with one line per done-when; labels `vallum,vallum-fix,priority::P0` if the tier is Core,
else `vallum,vallum-fix,priority::P1`.

Answer with JSON `{"postmortem": true, "learned_mr": <iid>, "fix_issue": <iid>}`.
