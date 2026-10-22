"""Custom aliases: pick your own short code. Card: promises/features/4-custom-alias.yaml"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from app import flags
from app.security import require_api_key
from app.store import LinkExists

router = APIRouter()

ALIAS_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")


class NewAlias(BaseModel):
    alias: str
    url: str

    @field_validator("alias")
    @classmethod
    def letters_digits_dashes(cls, value: str) -> str:
        if not ALIAS_PATTERN.fullmatch(value):
            raise ValueError("alias may only contain letters, digits and dashes (max 64 characters)")
        return value

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


@router.post(
    "/api/aliases",
    status_code=201,
    dependencies=[Depends(require_api_key), Depends(flags.require("custom_alias"))],
)
def create_alias(body: NewAlias, request: Request):
    try:
        link = request.app.state.store.create(body.alias, body.url)
    except LinkExists:
        raise HTTPException(status_code=409, detail="alias already taken")
    return _public(link, request)
