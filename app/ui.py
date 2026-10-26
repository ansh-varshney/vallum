"""The link shortener's look: a light, cream product page with a floating header, lime highlights and dark pill
buttons. The Vallum control room (the status site) is a separate service with its own look (control/ui.py)."""

import html
import os

CONTROL_ROOM = os.environ.get("CONTROL_ROOM_URL", "https://vallum-status-256944994567.us-central1.run.app").rstrip("/")

FONTS = ("https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700;12..96,800"
         "&family=Inter:wght@400;500;600;700&display=swap")

BASE_CSS = """
:root{color-scheme:light;--bg:#fbf8f1;--card:#fff;--soft:#f1ece1;--soft2:#f6f2ea;--line:#e8e1d3;--ink:#1c1a16;--ink2:#3c3a33;
--muted:#736d61;--dark:#171614;--lime:#d8f065;--lime-soft:#f2f8d4;--peach:#ffe6d6;--good:#2d8552;--good-soft:#e2f2e7;
--bad:#bf4630;--bad-soft:#f9e2db;--r:26px;--shadow:0 1px 2px rgba(45,35,15,.05),0 12px 32px rgba(45,35,15,.07);
--mono:ui-monospace,"SF Mono","Cascadia Code",Consolas,monospace}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh;
background-image:radial-gradient(900px 420px at 50% -120px,#fff3c9 0,rgba(255,243,201,0) 70%)}
a{color:var(--ink);text-decoration-thickness:1px;text-underline-offset:3px}
h1,h2,h3{font-family:"Bricolage Grotesque",Inter,sans-serif;margin:0;letter-spacing:-.03em;line-height:1.08}
.wrap{max-width:1140px;margin:0 auto;padding:0 20px}
.top{max-width:1140px;margin:16px auto 0;padding:0 20px}
.bar{display:flex;align-items:center;gap:16px;background:rgba(255,255,255,.9);border:1px solid var(--line);border-radius:22px;
padding:10px 10px 10px 18px;box-shadow:var(--shadow)}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;font-family:"Bricolage Grotesque",Inter,sans-serif;font-weight:800;font-size:21px;letter-spacing:-.03em;white-space:nowrap}
.motto{margin:0 auto;color:var(--muted);font-size:15px}
.cta{display:inline-flex;align-items:center;gap:8px;background:var(--dark);color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:10px 16px;border-radius:999px;white-space:nowrap}
.cta:hover{background:#2c2a26}.cta .brief{display:none}
@media (max-width:720px){.motto{display:none}.cta{margin-left:auto}}
@media (max-width:480px){.bar{padding:8px 8px 8px 12px;gap:8px}.brand{font-size:18px}.cta{padding:9px 13px;font-size:13px}.cta .long{display:none}.cta .brief{display:inline}}
button,.btn{font:inherit;font-weight:600;border:0;border-radius:999px;padding:12px 20px;cursor:pointer;background:var(--dark);color:#fff;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:8px;white-space:nowrap}
button:hover,.btn:hover{background:#2c2a26}button:disabled{opacity:.6;cursor:default}
button.ghost,.btn.ghost{background:var(--soft);color:var(--ink)}button.ghost:hover,.btn.ghost:hover{background:var(--line)}
input{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:12px 18px;width:100%;min-width:0}
input::placeholder{color:#a39c8e}
:focus-visible{outline:3px solid #c9e66a;outline-offset:2px}
.muted{color:var(--muted)}.small{font-size:13.5px}.mono{font-family:var(--mono);font-size:13px}
footer.foot{max-width:1140px;margin:40px auto 0;padding:24px 20px 44px;color:var(--muted);font-size:13.5px;display:flex;gap:10px 18px;flex-wrap:wrap;align-items:center;justify-content:center;text-align:center}
footer.foot a{color:var(--muted)}
"""

MARK = ('<svg viewBox="0 0 32 32" width="32" height="32" aria-hidden="true"><circle cx="16" cy="16" r="16" fill="#d8f065"/>'
        '<path d="M13.6 18.4l4.8-4.8M12.3 15.2l-1.5 1.5a3.4 3.4 0 004.8 4.8l1.5-1.5M19.7 16.8l1.5-1.5a3.4 3.4 0 00-4.8-4.8l-1.5 1.5" '
        'stroke="#1c1a16" stroke-width="2.2" fill="none" stroke-linecap="round"/></svg>')


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def header() -> str:
    return (f'<header class="top"><div class="bar"><a class="brand" href="/">{MARK}Vallum Links</a>'
            '<span class="motto">Short links. Shipped with care.</span>'
            f'<a class="cta" href="{CONTROL_ROOM}/" rel="noopener">How <span class="long">this app</span><span class="brief">it</span> ships <span aria-hidden="true">↗</span></a></div></header>')


def page(title: str, body: str, *, css: str = "", script: str = "", head: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">{head}
<style>{BASE_CSS}{css}</style></head><body>{body}{f"<script>{script}</script>" if script else ""}</body></html>"""
