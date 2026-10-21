"""Create, read and delete short links. Card: promises/features/base-links.yaml"""

import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.security import require_api_key
from app.store import LinkExists

router = APIRouter()

ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7


class NewLink(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def http_only(cls, value: str) -> str:
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")) or len(value) > 2048 or " " in value:
            raise ValueError("url must be an http(s) address under 2048 characters")
        return value


def _public(link: dict, request: Request) -> dict:
    return {
        "code": link["code"],
        "url": link["url"],
        "short_url": str(request.base_url).rstrip("/") + f"/r/{link['code']}",
        "created_at": link.get("created_at"),
    }


@router.post("/api/links", status_code=201, dependencies=[Depends(require_api_key)])
def create_link(body: NewLink, request: Request):
    store = request.app.state.store
    for _ in range(5):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        try:
            return _public(store.create(code, body.url), request)
        except LinkExists:
            continue
    raise HTTPException(status_code=503, detail="Could not allocate a code")


@router.get("/api/links/{code}")
def get_link(code: str, request: Request):
    link = request.app.state.store.get(code)
    if not link:
        raise HTTPException(status_code=404, detail="Not found")
    return _public(link, request)


@router.delete("/api/links/{code}", status_code=204, dependencies=[Depends(require_api_key)])
def delete_link(code: str, request: Request):
    if not request.app.state.store.delete(code):
        raise HTTPException(status_code=404, detail="Not found")
