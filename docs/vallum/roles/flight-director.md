# Flight Director notes for this repository

Extra instructions for Vallum's Flight Director in this codebase, on top of `skills/flight-director/SKILL.md`.
Owner-written.

- Feature Cards are `promises/features/<issue>-<feature>.yaml`; existing ones are listed in the code map. Don't
  open other cards to check the guardians' syntax: the promise checker does that on the merge request.
- Redirects (`app/features/redirect.py`, route `GET /r/{code}`) are Core. A request that touches them is Core.
- A change to the home page (`app/features/home.py`) is a `ui-page` change to an existing feature.
- Keep delegations short and complete: the issue number and title, the plan JSON, and for the Builder every
  guardian's promises verbatim.
