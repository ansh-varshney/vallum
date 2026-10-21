"""Health check. Card: promises/features/base-health.yaml"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request):
    return {"ok": True, "revision": request.app.state.revision}
