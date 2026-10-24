"""Link preview page: shows where a short link goes, with a Continue link. Card: promises/features/8-link-preview.yaml"""

import html
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import flags

router = APIRouter()

CODE_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")


def _page(url: str) -> str:
    safe = html.escape(url, quote=True)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Link preview</title>
<style>
body{{margin:0;background:#f7f8fb;color:#1f2a44;font:16px/1.5 system-ui,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:32px 16px}}
.dest{{background:#fff;border:1px solid #d9deea;border-radius:10px;padding:12px 16px;word-break:break-all}}
a.go{{display:inline-block;margin-top:16px;padding:10px 20px;background:#4f46e5;color:#fff;border-radius:8px;text-decoration:none}}
</style></head><body><main>
<h1>This short link goes to:</h1>
<p class="dest">{safe}</p>
<a class="go" href="{safe}" rel="noopener noreferrer">Continue</a>
</main></body></html>"""


@router.get("/p/{code}", response_class=HTMLResponse, dependencies=[Depends(flags.require("link_preview"))])
def preview(code: str, request: Request):
    if not CODE_PATTERN.fullmatch(code):
        raise HTTPException(status_code=404, detail="Not found")
    link = request.app.state.store.get(code)
    if not link:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(_page(link["url"]))
