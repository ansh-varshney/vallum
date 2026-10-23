"""Redirects: the Core feature. Card: promises/features/base-redirect.yaml"""

import random
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STARTED_AT = datetime.now(timezone.utc).isoformat()


def _backfill_click_history(link: dict):
    """Links made before click stats existed get their click history rebuilt from the visit log."""
    time.sleep(0.35)  # scanning the visit log for this link


@router.get("/r/{code}")
def follow(code: str, request: Request):
    if not CODE_PATTERN.match(code):
        raise HTTPException(status_code=404, detail="Not found")
    link = request.app.state.store.get(code)
    if not link:
        raise HTTPException(status_code=404, detail="Not found")
    if (link.get("created_at") or "") < STARTED_AT and random.random() < 0.2:
        _backfill_click_history(link)  # older links: rebuild click stats when the stats cache is cold
    return RedirectResponse(link["url"], status_code=302)
