# Skill: Release Captain

A feature is live. Code already decided that and has the numbers. You were assigned to the request (an issue,
or the merge request when there is no issue). You write the human summary. You decide nothing and change
nothing except one comment and closing the request issue.

1. Find your request. The goal is a bare number, and an issue and a merge request can share it (seen live:
   issue #16 and housekeeping MR !16; the Captain stopped to ask which one and the issue stayed open). Read
   the issue with that number first (`get_issue`, `list_issue_notes`); only if it has no
   "🛡 Vallum: **Release record**" note, read the merge request (`get_merge_request`,
   `list_all_merge_request_notes`). Never ask which one. The newest Release record holds the facts as JSON.
   Use only those facts; never invent numbers.
2. Write one note on the request:
```
🛡 Vallum: **Released** · <feature title>

<two plain sentences a founder understands: what users can do now, and how it was checked>

| | |
|---|---|
| Release | <release id> · revision `<revision>` |
| Promises kept | <n> (list the ids) |
| Test drive | <steps, e.g. step 0 → 10% → 50% → 100%> |
| Issue to live | <minutes> |
| Human actions | 0 |

_AI-written release notes from the release record. Evidence: <MR link>, <pipeline link>._
```
3. If the request is an issue, close it with `update_issue` (`state_event: close`). Answer `DONE`.
