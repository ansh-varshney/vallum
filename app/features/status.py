"""The old status address. The status pages live on their own site now, the Vallum control room (a separate Cloud
Run service with its own address); /status and the paths under it forward there, so old links keep working.
Card: promises/features/base-status.yaml"""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.ui import CONTROL_ROOM

router = APIRouter()

MOVED = {"rules": "/rules", "live.json": "/live.json"}


@router.get("/status")
def status_page():
    return RedirectResponse(f"{CONTROL_ROOM}/", status_code=302)


@router.get("/status/{rest:path}")
def status_subpage(rest: str):
    return RedirectResponse(CONTROL_ROOM + MOVED.get(rest, "/"), status_code=302)
