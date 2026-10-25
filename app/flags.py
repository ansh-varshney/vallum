"""Kill switches. GitLab Feature Flags are the source of truth; Vallum mirrors them into the state store
(vallum_state/flags) and the app reads that mirror, cached for a few seconds. A flag is on unless the mirror
says it is off, or it is listed in APP_FLAGS_OFF (local runs and tests)."""

import os
import time

from fastapi import HTTPException, Request

CACHE_SECONDS = 10
_cache = {"at": 0.0, "doc": {}}


def _mirror(store) -> dict:
    if store is None:
        return {}
    if time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["doc"]
    try:
        doc = store.read_doc("vallum_state", "flags") or {}
    except Exception:  # an unreachable store must never switch a feature off
        doc = _cache["doc"]
    _cache.update(at=time.monotonic(), doc=doc)
    return doc


def is_enabled(flag: str, store=None, revision: str | None = None) -> bool:
    off = {f.strip() for f in os.environ.get("APP_FLAGS_OFF", "").split(",") if f.strip()}
    if flag in off:
        return False
    doc = _mirror(store)
    if revision and (doc.get("for_revision") or {}).get(flag) == revision:
        return True          # a release being test-driven with its fix for a switched-off feature
    return (doc.get("flags") or {}).get(flag, True) is not False


def require(flag: str):
    """FastAPI dependency: the route answers 404 while its feature's flag is off."""

    def _check(request: Request):
        state = request.app.state
        if not is_enabled(flag, getattr(state, "store", None), getattr(state, "revision", None)):
            raise HTTPException(status_code=404, detail="Not found")

    return _check
