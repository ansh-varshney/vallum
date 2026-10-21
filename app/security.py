"""API key for write endpoints. Read from APP_API_KEY, or from the state store in the cloud,
where the test-drive controller creates it before the first deploy (no key in the repo or CI)."""

import hmac
import os

from fastapi import Header, HTTPException, Request

_cached = {}


def api_key(request: Request):
    if not _cached.get("key"):
        key = os.environ.get("APP_API_KEY")
        if not key:
            doc = request.app.state.store.read_doc("vallum_secrets", "app") or {}
            key = doc.get("api_key")
        _cached["key"] = key
    return _cached["key"]


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)):
    expected = api_key(request)
    if not expected:
        raise HTTPException(status_code=503, detail="API key not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key")
