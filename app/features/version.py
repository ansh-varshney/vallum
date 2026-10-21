"""Which version is answering: revision and feature list. Card: promises/features/test-version.yaml"""

from fastapi import APIRouter, Depends, Request

from app import flags

router = APIRouter()


@router.get("/api/version", dependencies=[Depends(flags.require("version"))])
def version(request: Request):
    return {"revision": request.app.state.revision, "features": request.app.state.features}
