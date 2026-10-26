"""Link limits: the longest URL and code the API accepts. Card: promises/features/16-limits.yaml"""

from fastapi import APIRouter, Depends

from app import flags

router = APIRouter()

MAX_URL_LENGTH = 2048
MAX_CODE_LENGTH = 64


@router.get("/api/limits", dependencies=[Depends(flags.require("limits"))])
def limits():
    return {"max_url_length": MAX_URL_LENGTH, "max_code_length": MAX_CODE_LENGTH}
