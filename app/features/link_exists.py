"""Tell whether a short link exists, without revealing where it goes. Card: promises/features/10-link-exists.yaml"""

import re

from fastapi import APIRouter, Depends, Request

from app import flags

router = APIRouter()

# Letters, digits and dashes, 1-64 characters (ASCII only).
CODE = re.compile(r"[A-Za-z0-9-]{1,64}")


@router.get("/api/links/{code}/exists", dependencies=[Depends(flags.require("link_exists"))])
def link_exists(code: str, request: Request):
    """Public and read-only: answers only the code and whether it exists, never the destination."""
    if not CODE.fullmatch(code):
        return {"code": code, "exists": False}
    link = request.app.state.store.get(code)
    return {"code": code, "exists": link is not None}
