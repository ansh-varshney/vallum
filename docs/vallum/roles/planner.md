# Planner notes for this repository

Extra instructions for Vallum's Planner in this codebase, on top of `skills/planner/SKILL.md`. Owner-written.

- The issue number is in the Flight Director's task. Read that issue; never guess another number.
- One feature per module: `app/features/<module>.py`, tests in `tests/test_<module>.py`, card in
  `promises/features/<issue>-<feature>.yaml`. The flag (kill switch) is the module name.
- Tiers here: anything on the redirect path is Core; API endpoints and pages are Standard unless the issue says
  Experimental. A change to an existing feature keeps that feature's card and tier.
- `app/main.py`, `app/store.py` and `app/security.py` are never edited by a feature; plan around them.
- List every error case in the plan with its status, so the guardians agree: a malformed or unknown code in the
  path answers 404; a bad request body 422; a duplicate 409; a missing or wrong key 401.
