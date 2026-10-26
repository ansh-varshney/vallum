"""The control room's look: an operations console, deliberately unlike the link app. A cool, light page with a side
navigation (one topic per page), compact cards, stat tiles and tables, and a small Markdown and YAML renderer for the
read-only copies of the repo's rules. Everything is escaped before formatting, so state or repository text can never
inject HTML."""

import html
import re
from datetime import datetime, timezone

FONTS = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700"
         "&family=IBM+Plex+Mono:wght@400;500&display=swap")

ICONS = {
    "overview": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
                '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "releases": '<path d="M4 17l6-6 4 4 6-6"/><path d="M14 9h6v6"/>',
    "features": '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
    "allowance": '<path d="M4 16a8 8 0 1116 0"/><path d="M12 16l4-5"/>',
    "security": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>',
    "rules": '<path d="M5 4h9a4 4 0 014 4v12H9a4 4 0 01-4-4V4z"/><path d="M9 9h5M9 13h5"/>',
    "out": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
}

TABS = (("overview", "/", "Overview"), ("releases", "/releases", "Releases"), ("features", "/features", "Features"),
        ("allowance", "/allowance", "Allowance"), ("security", "/security", "Security"), ("rules", "/rules", "Rules and code"))

CSS = """
:root{color-scheme:light;--bg:#f3f5f9;--surface:#fff;--sunken:#f6f8fb;--line:#e3e8ef;--line2:#d5dce6;--ink:#0f172a;--ink2:#334155;
--muted:#64748b;--brand:#4338ca;--brand-soft:#eef0ff;--brand-ink:#312e81;--good:#15803d;--good-soft:#e8f6ed;--warn:#b45309;
--warn-soft:#fdf3e2;--bad:#c2261d;--bad-soft:#fdeceb;--info:#475569;--info-soft:#eef1f6;--core:#1d4ed8;--standard:#0f8a7e;
--experimental:#a21caf;--r:12px;--shadow:0 1px 2px rgba(15,23,42,.04),0 1px 3px rgba(15,23,42,.04);
--mono:"IBM Plex Mono",ui-monospace,Consolas,monospace}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.55 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
code,pre,.mono,.chip,.yaml{font-variant-ligatures:none}
a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}
h1,h2,h3{margin:0;line-height:1.25;letter-spacing:-.01em}
.shell{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh;background:linear-gradient(90deg,var(--surface) 247px,var(--line) 247px,var(--line) 248px,var(--bg) 248px)}
.side{position:sticky;top:0;height:100vh;background:var(--surface);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:18px 14px}
.logo{display:flex;align-items:center;gap:11px;color:var(--ink);padding:4px 8px 18px;text-decoration:none!important}
.logo b{display:block;font-size:16px;font-weight:700}.logo small{display:block;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.09em}
.nav{display:flex;flex-direction:column;gap:2px}
.nav a{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:8px;color:var(--ink2);font-weight:500;position:relative;text-decoration:none}
.nav a svg{width:18px;height:18px;flex:none;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;color:var(--muted)}
.nav a:hover{background:var(--sunken)}.nav a.on{background:var(--brand-soft);color:var(--brand-ink);font-weight:600}
.nav a.on svg{color:var(--brand)}.nav a.on::before{content:"";position:absolute;left:-14px;top:8px;bottom:8px;width:3px;border-radius:0 3px 3px 0;background:var(--brand)}
.side .foot{margin-top:auto;display:flex;flex-direction:column;gap:10px}
.health{border:1px solid var(--line);border-radius:10px;padding:12px;font-size:13px;background:var(--sunken)}
.health .t{display:flex;gap:8px;align-items:center;font-weight:600;color:var(--ink)}.health p{margin:4px 0 0;color:var(--muted);font-size:12.5px}
.appbtn{display:flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line2);border-radius:8px;padding:9px 12px;color:var(--ink);font-weight:600;background:var(--surface)}
.appbtn svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.appbtn:hover{background:var(--sunken);text-decoration:none}
.content{min-width:0;display:flex;flex-direction:column}
main{padding:28px 32px 12px;max-width:1240px;width:100%}
footer.foot2{padding:18px 32px 32px;color:var(--muted);font-size:12.5px;display:flex;gap:6px 16px;flex-wrap:wrap;max-width:1240px}footer.foot2 a{color:var(--muted)}
@media (max-width:960px){.shell{grid-template-columns:minmax(0,1fr);background:var(--bg)}.side{min-width:0;position:sticky;top:0;z-index:5;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;padding:10px 12px;gap:6px;border-right:0;border-bottom:1px solid var(--line)}
.logo{padding:2px 6px}.nav{flex-direction:row;overflow-x:auto;width:100%;order:3;scrollbar-width:none}.nav::-webkit-scrollbar{display:none}
.nav a{white-space:nowrap;padding:7px 10px}.nav a.on::before{display:none}.side .foot{margin:0 0 0 auto;flex-direction:row}.health{display:none}
.appbtn{padding:6px 10px;font-size:13px}main{padding:20px 16px 8px}footer.foot2{padding:16px}}
.phead{display:flex;gap:16px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;margin:0 0 20px}
.phead h1{font-size:26px;font-weight:700}.phead p{margin:6px 0 0;color:var(--muted);max-width:720px}
.crumb{font-size:13px;color:var(--muted);margin-bottom:6px}.crumb a{color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);min-width:0}
.card>.hd{display:flex;gap:10px;align-items:baseline;justify-content:space-between;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid var(--line)}
.card>.hd h2{font-size:15px;font-weight:600}.card>.hd span{color:var(--muted);font-size:12.5px}.card>.bd{padding:16px 18px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid21{display:grid;grid-template-columns:1.6fr 1fr;gap:16px}
@media (max-width:1060px){.grid2,.grid21{grid-template-columns:1fr}}
.stack>*+*{margin-top:16px}
.status{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;padding:5px 10px;border-radius:999px;border:1px solid var(--line);background:var(--surface);white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.dot.good{background:var(--good)}.dot.warn{background:#d97706}.dot.bad{background:var(--bad)}.dot.teal,.dot.brand{background:var(--brand)}.dot.info{background:#94a3b8}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:6px;white-space:nowrap;line-height:1.6;background:var(--info-soft);color:var(--info)}
.pill.good{color:var(--good);background:var(--good-soft)}.pill.warn{color:var(--warn);background:var(--warn-soft)}
.pill.bad{color:var(--bad);background:var(--bad-soft)}.pill.teal,.pill.brand{color:var(--brand-ink);background:var(--brand-soft)}.pill.lime{color:#3f6212;background:#ecfccb}
.pill.core{color:var(--core);background:#e8efff}.pill.standard{color:var(--standard);background:#e2f5f2}.pill.experimental{color:var(--experimental);background:#fbe8fb}
.muted{color:var(--muted)}.small{font-size:12.5px}.mono{font-family:var(--mono);font-size:12px}
.chip{display:inline-flex;font-family:var(--mono);font-size:11.5px;padding:2px 7px;border-radius:6px;background:var(--sunken);border:1px solid var(--line);color:var(--ink2)}
.table{width:100%;border-collapse:collapse;font-size:13.5px}
.table th{text-align:left;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--sunken);padding:9px 14px;border-bottom:1px solid var(--line)}
.table td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top}.table tr:last-child td{border-bottom:0}
.table tr.link{cursor:pointer}.table tr.link:hover td{background:#fafbfe}
.empty{color:var(--muted);background:var(--sunken);border:1px dashed var(--line2);border-radius:10px;padding:14px 16px;margin:0}
.note{border-radius:10px;padding:11px 14px;margin:0 0 16px;font-weight:500;border:1px solid}.note.bad{background:var(--bad-soft);color:var(--bad);border-color:#f7c9c5}
.note.info{background:var(--brand-soft);color:var(--brand-ink);border-color:#dcdffe}
.more{font-weight:600;font-size:13px}
.md h1{font-size:22px;margin:4px 0 12px}.md h2{font-size:18px;margin:24px 0 8px}.md h3{font-size:15px;margin:18px 0 6px}
.md p,.md ul,.md ol{margin:0 0 10px}.md li{margin:3px 0}.md code{font-family:var(--mono);font-size:12px;background:var(--sunken);border:1px solid var(--line);padding:0 5px;border-radius:5px}
.md pre,.yaml{font-family:var(--mono);font-size:12px;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:14px 16px;overflow:auto;line-height:1.6;margin:0}
.md pre{margin:0 0 12px}.md pre code{background:none;border:0;padding:0;color:inherit}.md blockquote{margin:0 0 10px;padding:6px 14px;border-left:3px solid var(--brand);color:var(--muted)}
.md table{border-collapse:collapse;margin:0 0 14px;font-size:12.5px;display:block;overflow:auto;max-width:100%;width:max-content}
.md th,.md td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top;max-width:380px;overflow-wrap:break-word;min-width:70px}
.md td:last-child{min-width:260px}.md th{background:var(--sunken)}
.yaml .k{color:#a5b4fc}.yaml .c{color:#94a3b8;font-style:italic}.yaml .s{color:#86efac}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
"""

MARK = ('<svg viewBox="0 0 32 32" width="34" height="34" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#4338ca"/>'
        '<path d="M7 23V12h3.4v3.2h3.3V12h4.6v3.2h3.3V12H25v11z" fill="#fff"/></svg>')


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def when(value) -> str:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    return t.astimezone(timezone.utc).strftime("%d %b %H:%M UTC")


def pill(text: str, tone: str = "") -> str:
    return f'<span class="pill {tone}">{esc(text)}</span>'


def mode_tone(mode: str | None) -> str:
    return {"green": "good", "yellow": "warn", "red": "bad"}.get((mode or "green").lower(), "info")


def icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def page(title: str, active: str, body: str, *, app_url: str, links: dict | None = None, health: tuple | None = None,
         css: str = "", script: str = "") -> str:
    """The console shell: side navigation with one entry per topic, the page, and a footer with the sources."""
    nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}"{" aria-current=page" if key == active else ""}>'
                  f'{icon(key)}{label}</a>' for key, href, label in TABS)
    links = links or {}
    tone, text = health or ("info", "Status unknown")
    foot = ["Visitors are simulated by CI jobs during the hackathon."]
    if links.get("project_url"):
        foot.append(f'<a href="{esc(links["project_url"])}" rel="noopener">Source and pipelines on GitLab</a>')
    if links.get("ledger_url"):
        foot.append(f'<a href="{esc(links["ledger_url"])}" rel="noopener">The Ledger</a>')
    foot.append("Read-only: this site changes nothing.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Vallum control room</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}"><style>{CSS}{css}</style></head><body><div class="shell">
<aside class="side"><a class="logo" href="/">{MARK}<span><b>Vallum</b><small>Control room</small></span></a>
<nav class="nav" aria-label="Sections">{nav}</nav>
<div class="foot"><div class="health"><div class="t"><span class="dot {tone}"></span>{esc(text)}</div>
<p>Agents propose, code decides.</p></div>
<a class="appbtn" href="{esc(app_url)}/" rel="noopener">{icon("out")}Open the link app</a></div></aside>
<div class="content"><main>{body}</main>
<footer class="foot2">{"".join(f"<span>{f}</span>" for f in foot)}</footer></div></div>
{f"<script>{script}</script>" if script else ""}</body></html>"""


# ---------------------------------------------------------------- Markdown and YAML (escaped first)
def _inline(text: str, link_base: str = "") -> str:
    out = esc(text)
    out = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"(?<!\w)_([^_\s][^_]*?)_(?!\w)", r"<i>\1</i>", out)
    out = out.replace("&lt;br&gt;", "<br>")          # the code map breaks table cells with <br>

    def link(m):
        label, href = m.group(1), html.unescape(m.group(2))
        if not re.match(r"https?://", href):
            if not link_base or href.startswith("#"):
                return label
            href = f"{link_base.rstrip('/')}/{href.lstrip('./')}"
        return f'<a href="{esc(href)}" rel="noopener">{label}</a>'

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, out)


def markdown(text: str, link_base: str = "") -> str:
    """Headings, paragraphs, lists, tables, quotes and fenced code: what the repo's docs use."""
    lines, out, i = text.replace("\r\n", "\n").split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            code, i = [], i + 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{esc(chr(10).join(code))}</code></pre>")
            i += 1
            continue
        m = re.match(r"(#{1,4})\s+(.*)", line)
        if m:
            level = min(len(m.group(1)), 3)
            out.append(f"<h{level}>{_inline(m.group(2), link_base)}</h{level}>")
            i += 1
            continue
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in line.strip().strip("|").split("|")]
            rows, i = [], i + 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{_inline(c, link_base)}</th>" for c in head)
            trs = "".join("<tr>" + "".join(f"<td>{_inline(c, link_base)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
            continue
        if re.match(r"\s*([-*]|\d+\.)\s+", line):
            ordered = bool(re.match(r"\s*\d+\.", line))
            items = []
            while i < len(lines) and (re.match(r"\s*([-*]|\d+\.)\s+", lines[i]) or (lines[i].startswith("  ") and items)):
                item = re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i]) if re.match(r"\s*([-*]|\d+\.)\s+", lines[i]) else None
                if item is None:
                    items[-1] += " " + lines[i].strip()
                else:
                    items.append(item)
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t, link_base)}</li>" for t in items) + f"</{tag}>")
            continue
        if line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip("> "))
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(quote), link_base)}</blockquote>")
            continue
        if not line.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,4}\s|```|>|\s*([-*]|\d+\.)\s+|\s*\|)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append(f"<p>{_inline(' '.join(para), link_base)}</p>")
        else:
            out.append(f"<p>{_inline(line.strip(), link_base)}</p>")
            i += 1
    return '<div class="md">' + "".join(out) + "</div>"


def yaml_html(text: str) -> str:
    """YAML with keys, comments and quoted strings tinted; escaped first."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = esc(raw)
        body, comment = line, ""
        m = re.search(r"(^|\s)(#.*)$", line)
        if m and line.count("&quot;", 0, m.start()) % 2 == 0:
            body, comment = line[:m.start(2)], f'<span class="c">{m.group(2)}</span>'
        body = re.sub(r"^(\s*-?\s*)([\w.-]+)(:)(\s|$)", r'\1<span class="k">\2</span>\3\4', body)
        body = re.sub(r"(&quot;[^&]*?&quot;)", r'<span class="s">\1</span>', body)
        lines.append(body + comment)
    return '<pre class="yaml">' + "\n".join(lines) + "</pre>"
