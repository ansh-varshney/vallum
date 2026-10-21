"""Health check at /health (Cloud Run reserves /healthz). Card: promises/features/base-health.yaml"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request):
    return {"ok": True, "revision": request.app.state.revision}
