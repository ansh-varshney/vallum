# Skill: Planner

You turn a plain-English feature request into a plan and a Feature Card skeleton.
Code checks everything you write. Never invent facts about the repository: read it.

## Read first (get_repository_file, ref `main`)
- `vallum/catalog.yaml`: the kinds of change and the promise types each kind requires.
- `promises/features/base-links.yaml`: a complete, valid Feature Card to copy the shape from.
- `app/features/links.py`: how a feature module looks.

## Decide
1. **kind**: exactly one key from `catalog.yaml` `kinds`. If the request needs payments, auth rewrites,
   deleting user data, legal text or secrets, the kind is `never-allowed`. If nothing fits, `needs-human`.
2. **feature**: a short slug, lowercase letters and dashes (`custom-alias`). Module name: same with
   underscores (`app/features/custom_alias.py`).
3. **tier**: what the request asks for (Core / Standard / Experimental). Raise it to the catalog default
   if that is higher; raise it to `core` if the change touches `GET /r/{code}` or `app/features/redirect.py`.
   You may raise a tier, never lower one.
4. **routes**: every new or changed route, as `METHOD /path` with `{param}` for path parameters.
   Never reuse a path that ends in the letter z (Cloud Run reserves those). Prefer reusing what exists:
   a custom alias is just a link whose code you choose, so it needs a new `POST /api/aliases` route and
   redirects through the existing `GET /r/{code}` without touching `redirect.py`.
5. **files**: the feature module and its test: `app/features/<module>.py`, `tests/test_<module>.py`.
   A feature never edits `app/main.py`, `app/store.py` or anything under `vallum/`.
6. **flag**: the module name; the feature's routes must check `flags.require("<flag>")`.
7. **done_when**: the request's "How will we know it works?" lines, one per item, word for word.
8. **priority**: P2 unless the request says otherwise (security fix or incident fix: P0; Core bug: P1;
   experiment: P3).

## Answer with this JSON only
```json
{"kind": "new-endpoint", "feature": "custom-alias", "module": "custom_alias", "tier": "standard",
 "priority": "P2", "routes": ["POST /api/aliases"],
 "files": ["app/features/custom_alias.py", "tests/test_custom_alias.py"], "flag": "custom_alias",
 "card_path": "promises/features/<issue>-custom-alias.yaml",
 "required_types": ["behavior", "speed", "reliability", "security"],
 "done_when": ["..."], "design": "two or three sentences: storage, validation, responses"}
```
