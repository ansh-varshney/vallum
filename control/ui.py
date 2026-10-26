"""The control room's look: a light, cream page with a floating header of tabs, one topic per page, and a small
Markdown and YAML renderer for the read-only copies of the repo's rules. Everything is escaped before formatting,
so state or repository text can never inject HTML."""

import html
import re
from datetime import datetime, timezone

FONTS = ("https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800"
         "&family=JetBrains+Mono:wght@400;500&display=swap")

TABS = (("overview", "/", "Overview"), ("releases", "/releases", "Releases"), ("features", "/features", "Features"),
        ("allowance", "/allowance", "Allowance"), ("security", "/security", "Security"), ("rules", "/rules", "Rules"))

CSS = """
:root{color-scheme:light;--bg:#f7f5ef;--surface:#fff;--soft:#eeebe3;--soft2:#f4f1ea;--line:#e4dfd3;--ink:#1a1915;
--ink2:#3b3933;--muted:#6f6a5f;--dark:#161512;--teal:#1d8a9f;--teal-soft:#dcf0f3;--lime:#d6ee5c;--lime-soft:#f0f7cf;
--good:#2c8653;--good-soft:#e1f1e6;--warn:#a87410;--warn-soft:#f7ecd0;--bad:#bf4630;--bad-soft:#f8e0d9;--info:#58688a;
--info-soft:#e7ebf3;--core:#1e5f74;--standard:#3a9fb4;--experimental:#9479c0;--r:24px;
--shadow:0 1px 2px rgba(40,32,16,.04),0 10px 30px rgba(40,32,16,.06);--mono:"JetBrains Mono",ui-monospace,Consolas,monospace}
code,pre,.mono,.chip,.yaml{font-variant-ligatures:none}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15.5px/1.6 Manrope,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh}
body::before{content:"";display:block;height:6px;background:linear-gradient(90deg,#a5e2ec,#5ec2d4 45%,var(--lime))}
a{color:var(--teal);text-decoration-thickness:1px;text-underline-offset:3px}
h1,h2,h3{margin:0;letter-spacing:-.025em;line-height:1.15}
.wrap{max-width:1200px;margin:0 auto;padding:0 20px}
.top{max-width:1200px;margin:14px auto 0;padding:0 20px;position:sticky;top:10px;z-index:5}
.bar{display:flex;align-items:center;gap:16px;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);border:1px solid var(--line);
border-radius:22px;padding:10px 10px 10px 18px;box-shadow:var(--shadow)}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;font-weight:800;font-size:19px;letter-spacing:-.03em;white-space:nowrap}
.brand small{font-weight:600;font-size:12px;color:var(--muted);letter-spacing:0;padding:2px 8px;border-radius:999px;background:var(--soft2)}
.tabs{display:flex;gap:2px;margin:0 auto;overflow-x:auto;scrollbar-width:none}.tabs::-webkit-scrollbar{display:none}
.tabs a{color:var(--muted);text-decoration:none;font-weight:600;font-size:14.5px;padding:8px 14px;border-radius:999px;white-space:nowrap}
.tabs a:hover{color:var(--ink);background:var(--soft2)}.tabs a.on{color:var(--ink);background:var(--soft)}
.cta{display:inline-flex;align-items:center;gap:8px;background:var(--dark);color:#fff;text-decoration:none;font-weight:700;font-size:14px;
padding:10px 16px;border-radius:999px;white-space:nowrap}.cta:hover{background:#2b2a25}
@media (max-width:480px){.brand small{display:none}}
@media (max-width:900px){.bar{flex-wrap:wrap;padding:10px 10px 6px 14px;gap:8px}.tabs{order:3;width:100%;margin:0}.cta{margin-left:auto;padding:8px 13px}
.top{position:static}}
main{padding:46px 0 10px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:700;padding:6px 13px;border-radius:999px;background:var(--surface);border:1px solid var(--line)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.dot.good{background:var(--good);box-shadow:0 0 0 3px var(--good-soft)}.dot.warn{background:var(--warn);box-shadow:0 0 0 3px var(--warn-soft)}
.dot.bad{background:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)}.dot.teal{background:var(--teal);box-shadow:0 0 0 3px var(--teal-soft)}
.dot.info{background:var(--info)}
.phead{margin:6px 0 26px}.phead h1{font-size:clamp(30px,4.2vw,46px);font-weight:800;margin:12px 0 10px}
.phead p{color:var(--muted);font-size:17px;max-width:720px;margin:0}
.crumb{font-size:14px;color:var(--muted)}.crumb a{color:var(--muted)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:24px;box-shadow:var(--shadow)}
.panel>h2,.h2{font-size:21px;font-weight:800;margin:0 0 4px}.panel .sub{color:var(--muted);font-size:14px;margin:0 0 16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
@media (max-width:900px){.grid2,.grid3{grid-template-columns:1fr}}
.stack>*+*{margin-top:20px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:700;padding:3px 10px;border-radius:999px;white-space:nowrap;line-height:1.5;background:var(--soft2);color:var(--ink2)}
.pill.good{color:var(--good);background:var(--good-soft)}.pill.warn{color:var(--warn);background:var(--warn-soft)}
.pill.bad{color:var(--bad);background:var(--bad-soft)}.pill.info{color:var(--info);background:var(--info-soft)}
.pill.teal{color:#13697a;background:var(--teal-soft)}.pill.lime{color:#4b5a07;background:var(--lime-soft)}
.pill.core{color:#fff;background:var(--core)}.pill.standard{color:#fff;background:var(--standard)}.pill.experimental{color:#fff;background:var(--experimental)}
.muted{color:var(--muted)}.small{font-size:13.5px}.mono{font-family:var(--mono);font-size:12.5px}
.chip{display:inline-flex;font-family:var(--mono);font-size:12px;padding:3px 9px;border-radius:9px;background:var(--soft2);border:1px solid var(--line);color:var(--ink2)}
.list{list-style:none;margin:0;padding:0}.list>li{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--line)}
.list>li:first-child{border-top:0;padding-top:2px}
.empty{color:var(--muted);background:var(--soft2);border-radius:16px;padding:16px 18px;margin:0}
.note{border-radius:16px;padding:14px 18px;margin:0 0 20px;font-weight:600}.note.bad{background:var(--bad-soft);color:var(--bad)}
.note.info{background:var(--teal-soft);color:#13697a}
.more{font-weight:700;text-decoration:none;font-size:14px}
footer.foot{max-width:1200px;margin:30px auto 0;padding:22px 20px 44px;color:var(--muted);font-size:13.5px;display:flex;gap:8px 18px;flex-wrap:wrap;border-top:1px solid var(--line)}
footer.foot a{color:var(--muted)}
.md h1{font-size:26px;margin:4px 0 12px}.md h2{font-size:20px;margin:26px 0 8px}.md h3{font-size:16px;margin:20px 0 6px}
.md p,.md ul,.md ol{margin:0 0 10px}.md li{margin:3px 0}.md code{font-family:var(--mono);font-size:12.5px;background:var(--soft2);padding:1px 6px;border-radius:6px}
.md pre,.yaml{font-family:var(--mono);font-size:12.5px;background:var(--soft2);border:1px solid var(--line);border-radius:16px;padding:16px;overflow:auto;line-height:1.55;margin:0}
.md pre{margin:0 0 12px}.md pre code{background:none;padding:0}.md blockquote{margin:0 0 10px;padding:6px 14px;border-left:3px solid var(--teal);color:var(--muted)}
.md table{border-collapse:collapse;margin:0 0 14px;font-size:13px;display:block;overflow:auto;max-width:100%;width:max-content}
.md th,.md td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top;max-width:380px;overflow-wrap:break-word;min-width:70px}
.md td:last-child{min-width:260px}.md th{background:var(--soft2)}
.yaml .k{color:#13697a;font-weight:500}.yaml .c{color:var(--muted);font-style:italic}.yaml .s{color:var(--good)}
:focus-visible{outline:3px solid #9fd8e2;outline-offset:2px}
"""

MARK = ('<svg viewBox="0 0 32 32" width="30" height="30" aria-hidden="true"><rect width="32" height="32" rx="10" fill="#161512"/>'
        '<path d="M7 23V12h3.4v3.2h3.3V12h4.6v3.2h3.3V12H25v11z" fill="#d6ee5c"/></svg>')


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


def page(title: str, active: str, body: str, *, app_url: str, links: dict | None = None, css: str = "",
         script: str = "") -> str:
    """The page shell: the header with one tab per topic, the body, and a footer with the sources."""
    tabs = "".join(f'<a href="{href}" class="{"on" if key == active else ""}"'
                   f'{" aria-current=page" if key == active else ""}>{label}</a>' for key, href, label in TABS)
    links = links or {}
    foot = ["Visitors are simulated by CI jobs during the hackathon."]
    if links.get("project_url"):
        foot.append(f'<a href="{esc(links["project_url"])}" rel="noopener">Source and pipelines on GitLab</a>')
    if links.get("ledger_url"):
        foot.append(f'<a href="{esc(links["ledger_url"])}" rel="noopener">The Ledger</a>')
    foot.append("This site reads only public state and changes nothing.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Vallum control room</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}"><style>{CSS}{css}</style></head><body>
<header class="top"><div class="bar"><a class="brand" href="/">{MARK}Vallum <small>control room</small></a>
<nav class="tabs" aria-label="Sections">{tabs}</nav>
<a class="cta" href="{esc(app_url)}/" rel="noopener">Open the app <span aria-hidden="true">↗</span></a></div></header>
<main class="wrap">{body}</main>
<footer class="foot">{"".join(f"<span>{f}</span>" for f in foot)}</footer>
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
