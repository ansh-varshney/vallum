# Coding guidelines: link shortener

Written by the owner for people and for Vallum's agents. Everything specific to this codebase lives here
(and in `vallum/project.yaml`); Vallum's skills only hold role know-how. Read this and
[`CODEMAP.md`](CODEMAP.md) before planning, writing, reviewing or fixing anything. Protected path.

## What the product is
Small businesses create short links (`https://…/r/<code>`) for campaigns. Visitors follow them; owners
create, read and delete links through a small JSON API protected by an API key. Redirects are the feature
everything depends on (Core tier); link management is Standard; new extras start Experimental.

## Architecture
- **FastAPI app** in `app/`. `app/main.py` only builds the app and discovers features: every module in
  `app/features/` that exposes `router = APIRouter()` is mounted automatically. Never edit `main.py` for a feature.
- **Storage** (`app/store.py`): Firestore in the cloud and in CI (emulator), memory in unit tests. Use only:
  `store.create(code, url)` (raises `LinkExists` if the code is taken), `store.get(code)` (dict or `None`),
  `store.delete(code)` (bool). Reach it as `request.app.state.store`. A link is `{code, url, created_at}`.
- **API key** (`app/security.py`): write routes add `dependencies=[Depends(require_api_key)]`.
- **Kill switches** (`app/flags.py`): every route of a new feature adds `Depends(flags.require("<flag>"))`.
- **Headers**: middleware adds `X-Revision` and `X-Route` to every response; don't set them yourself.
- **Redirect route**: `GET /r/{code}` in `app/features/redirect.py` serves any stored code, so features that
  create links with chosen codes (for example custom aliases) never need to touch the redirect module.

## How to add a feature
1. New module `app/features/<module>.py` with `router = APIRouter()`; one module per feature.
2. Validate input with pydantic models and `field_validator` (bad input answers 422). Duplicates answer 409.
   URLs must be `http://` or `https://`, at most 2048 characters, with a host, and no whitespace or control
   characters (copy the validator in `app/features/links.py`).
3. Codes and aliases: letters, digits and dashes only, 1–64 characters, `fullmatch`. Never end a path segment
   you invent with the letter `z` at the top level (Cloud Run reserves paths like `/healthz`).
4. Return the same public link shape as `links.py`: `{code, url, short_url, created_at}`.
5. Tests in `tests/test_<module>.py` using the `client` fixture from `tests/conftest.py` and the header
   `{"X-API-Key": "test-key"}`; include the flag-off case (expect 404).
6. Feature Card in `promises/features/<issue>-<feature>.yaml` (see `promises/features/base-links.yaml`).

## Promise examples that fit this app
```yaml
- id: alias-redirects
  says: "A custom alias sends people to the right page"
  steps:
    - "POST /api/aliases {url: 'https://example.com/{rand}', alias: 'promo-{rand}'} -> 201"
    - "GET /r/promo-{rand} -> 302 https://example.com/{rand}"
  where: [mr, drive, live]
  severity: blocker
- id: alias-create-fast
  says: "95% of alias creations finish in under 300 ms"
  measure: "p95(latency, route='POST /api/aliases') < 300ms"
  where: [mr, drive, live]
  severity: major
```
Speed limits by tier here: Core 150 ms, Standard 300 ms, Experimental 500 ms (p95). Error rates: Core under
0.1%, Standard under 0.5%, Experimental under 1%. Popular links are tagged `popular == 1` in measurements.

## Never
- Edit `app/main.py`, `app/store.py`, `app/security.py`, another feature's module, or anything protected
  (`vallum/`, `.gitlab-ci.yml`, `.gitlab/duo/`, `skills/`, `docs/vallum/GUIDELINES.md`, `AGENTS.md`,
  `CODEOWNERS`, `promises/service.yaml`, `promises/learned/`).
- Log visitor IP addresses or read `vallum_secrets` from a feature.
- Do slow work on the redirect path; anything per-click must stay fast for popular links.
