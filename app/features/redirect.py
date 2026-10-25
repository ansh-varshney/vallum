"""Redirects: the Core feature. Card: promises/features/base-redirect.yaml"""
# Core feature: keep this path fast; any per-click work (previews, stats) must stay off the redirect path.

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.get("/r/{code}")
def follow(code: str, request: Request):
    if not CODE_PATTERN.match(code):
        raise HTTPException(status_code=404, detail="Not found")
    link = request.app.state.store.get(code)
    if not link:
        raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(link["url"], status_code=302)
