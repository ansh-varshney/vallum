# Builder notes for this repository

Extra instructions for Vallum's Builder in this codebase, on top of `skills/builder/SKILL.md`. Owner-written.

- Read `app/features/links.py` before writing a module: copy its URL validator and its response shape.
- A new feature that creates links with a chosen code calls `store.create(code, url)` and maps `LinkExists`
  to 409 with the detail "alias already taken" (or the feature's equivalent).
- Every test file includes a kill-switch test: set `APP_FLAGS_OFF=<flag>` with `monkeypatch.setenv` and
  expect 404.
- Keep anything that runs per redirect cheap: no extra Firestore round trips on `GET /r/{code}`.
- Copy the guardians' promises into the card as they are, except: a promise that expects 404 gets `guard: true`
  (it also passes on main, where the route doesn't exist), and a flag-off promise is dropped (the tests cover it).
- In fix mode, an owner's comment answering a needs-human question is a decision: change the card to match it.
