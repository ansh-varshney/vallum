"""Kill switches. Layer 4 connects this to GitLab Feature Flags (Unleash); until then
a flag is on unless listed in APP_FLAGS_OFF."""

import os

from fastapi import HTTPException


def is_enabled(flag: str) -> bool:
    off = {f.strip() for f in os.environ.get("APP_FLAGS_OFF", "").split(",") if f.strip()}
    return flag not in off


def require(flag: str):
    """FastAPI dependency: the route answers 404 while its feature's flag is off."""

    def _check():
        if not is_enabled(flag):
            raise HTTPException(status_code=404, detail="Not found")

    return _check
