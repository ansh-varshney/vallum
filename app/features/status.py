"""Public status page (a first version; Layer 4 adds promises and allowance bars).
Reads only the state store's public documents. Card: promises/features/base-status.yaml"""

import html
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

_cache = {"at": 0.0, "data": None}
CACHE_SECONDS = 30
TIERS = ("core", "standard", "experimental", "app")


def _load(store):
    if _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        data = {
            "release": store.read_doc("vallum_state", "release") or {},
            "modes": store.read_doc("vallum_state", "modes") or {},
            "recent": (store.read_doc("vallum_state", "recent") or {}).get("events", []),
        }
    except Exception:  # the page must render even if the state store is unreachable
        data = {"release": {}, "modes": {}, "recent": [], "error": True}
    _cache.update(at=time.monotonic(), data=data)
    return data


def _row(*cells):
    return "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in cells) + "</tr>"


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    data = _load(request.app.state.store)
    release = data["release"]
    modes = data["modes"]
    tier_rows = "".join(
        _row(t, f"{round(100 * modes.get(t, {}).get('allowance', 1.0))}%", modes.get(t, {}).get("mode", "green").upper())
        for t in TIERS
    )
    recent_rows = "".join(
        _row(e.get("at", "")[:19].replace("T", " "), e.get("kind", ""), e.get("summary", "")) for e in data["recent"][:10]
    ) or _row("-", "-", "No releases yet")
    slot = release.get("slot", "idle")
    current = release.get("feature") or "-"
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Vallum Status</title>
<style>
:root{{--bg:#f7f8fb;--fg:#1f2a44;--muted:#5b6785;--card:#fff;--line:#d9deea;--accent:#4f46e5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151a2c;--fg:#e8ecf7;--muted:#a5b0cc;--card:#1e2540;--line:#323b5c;--accent:#a5b4fc}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 16px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 14px}}
h2{{font-size:15px;margin:0 0 8px;color:var(--accent)}}
table{{width:100%;border-collapse:collapse}} td{{padding:5px 6px;border-top:1px solid var(--line)}}
</style></head><body><main>
<h1>🛡 Vallum status</h1>
<p>Revision {html.escape(request.app.state.revision)}. Visitors are simulated by CI jobs during the hackathon.</p>
<section><h2>Mistake allowance by tier</h2><table>{tier_rows}</table></section>
<section><h2>Release</h2><table>{_row("Release slot", slot)}{_row("Current release", current)}</table></section>
<section><h2>Recent events</h2><table>{recent_rows}</table></section>
</main></body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "public, max-age=30"})
