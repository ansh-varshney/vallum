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
body{{margin:0;background:#fbf8f1;color:#1c1a16;font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:640px;margin:0 auto;padding:56px 20px}}
h1{{font-size:28px;letter-spacing:-.02em;margin:0 0 16px}}
.dest{{background:#fff;border:1px solid #e8e1d3;border-radius:18px;padding:14px 18px;word-break:break-all;box-shadow:0 8px 24px rgba(45,35,15,.06)}}
a.go{{display:inline-block;margin-top:18px;padding:11px 22px;background:#171614;color:#fff;border-radius:999px;text-decoration:none;font-weight:600}}
a.home{{display:inline-block;margin:18px 0 0 12px;color:#736d61}}
</style></head><body><main>
<h1>This short link goes to:</h1>
<p class="dest">{safe}</p>
<a class="go" href="{safe}" rel="noopener noreferrer">Continue</a><a class="home" href="/">Back to Vallum Links</a>
</main></body></html>"""


@router.get("/p/{code}", response_class=HTMLResponse, dependencies=[Depends(flags.require("link_preview"))])
def preview(code: str, request: Request):
    if not CODE_PATTERN.fullmatch(code):
        raise HTTPException(status_code=404, detail="Not found")
    link = request.app.state.store.get(code)
    if not link:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(_page(link["url"]))
