"""Custom aliases: pick your own short code. Card: promises/features/4-custom-alias.yaml"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from app import flags
from app.security import require_api_key
from app.store import LinkExists

router = APIRouter()

ALIAS_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")


def _host(url: str) -> str:
    """The host part of an http(s) url, or \"\" when there is none."""
    rest = url.split("://", 1)[1]
    netloc = re.split(r"[/?#]", rest, maxsplit=1)[0]
    netloc = netloc.rsplit("@", 1)[-1]
    if netloc.startswith("["):
        return netloc.split("]", 1)[0].lstrip("[")
    return netloc.split(":", 1)[0]


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
        if not value.lower().startswith(("http://", "https://")) or len(value) > 2048:
            raise ValueError("url must be an http(s) address under 2048 characters")
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("url must not contain whitespace or control characters")
        if not _host(value):
            raise ValueError("url must have a host after the scheme")
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
