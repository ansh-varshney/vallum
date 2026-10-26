"""Shared look for the app's pages (home and status): a warm, cozy palette with light and dark themes, the page
shell, and a small Markdown and YAML renderer for the status page's read-only copies of the repo's rules.
Everything is escaped before formatting, so repository text can never inject HTML."""

import html
import re

FONTS = ("https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,650"
         "&family=Inter:wght@400;500;600&display=swap")

BASE_CSS = """
:root{--bg:#f8f3ec;--bg2:#f2e9dd;--card:#fffdf9;--ink:#2e2823;--muted:#7b6e62;--line:#e8dccd;--shadow:0 1px 2px rgba(74,52,32,.06),0 8px 24px rgba(74,52,32,.07);
--accent:#c4643c;--accent-ink:#fff8f2;--accent-soft:#f6e1d4;--good:#4f8a63;--good-soft:#e1efe4;--warn:#b98216;--warn-soft:#f8ecd2;
--bad:#b4472f;--bad-soft:#f8dfd8;--info:#59688a;--info-soft:#e6e9f2;--core:#c4643c;--standard:#59688a;--experimental:#8a6aa6;
--radius:18px;--mono:ui-monospace,"SF Mono","Cascadia Code",Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#1b1815;--bg2:#221e1a;--card:#27221e;--ink:#f3ece4;--muted:#b4a799;--line:#3b342d;
--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.25);--accent:#e88c62;--accent-ink:#2a1a10;--accent-soft:#4a2f22;
--good:#8cc6a0;--good-soft:#203a2a;--warn:#e7b85c;--warn-soft:#3f3218;--bad:#ee8f7d;--bad-soft:#4a2620;--info:#a9b6d6;
--info-soft:#2b3247;--core:#e88c62;--standard:#a9b6d6;--experimental:#c6a6e2}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:2px}
h1,h2,h3{font-family:Fraunces,Georgia,serif;font-weight:650;letter-spacing:-.01em;margin:0}
.wrap{max-width:1120px;margin:0 auto;padding:0 18px}
.nav{display:flex;align-items:center;gap:14px;padding:18px 0;flex-wrap:wrap}.brand{white-space:nowrap}
@media (max-width:520px){.nav .links{margin-left:0;width:100%}.nav .links a{padding:6px 8px}}
.brand{display:flex;align-items:center;gap:10px;font-family:Fraunces,Georgia,serif;font-weight:650;font-size:20px;color:var(--ink);text-decoration:none}
.brand .mark{width:30px;height:30px;border-radius:10px;background:var(--accent);color:var(--accent-ink);display:grid;place-items:center;font-size:16px}
.nav .links{margin-left:auto;display:flex;gap:4px;flex-wrap:wrap}
.nav .links a{color:var(--muted);text-decoration:none;padding:6px 10px;border-radius:10px;font-weight:500}
.nav .links a:hover,.nav .links a.on{background:var(--bg2);color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap;line-height:1.6}
.pill.good{color:var(--good);background:var(--good-soft)}.pill.warn{color:var(--warn);background:var(--warn-soft)}
.pill.bad{color:var(--bad);background:var(--bad-soft)}.pill.info{color:var(--info);background:var(--info-soft)}
.pill.accent{color:var(--accent);background:var(--accent-soft)}
.muted{color:var(--muted)}.small{font-size:13px}.mono{font-family:var(--mono);font-size:13px}
button,.btn{font:inherit;font-weight:600;border:0;border-radius:12px;padding:10px 16px;cursor:pointer;background:var(--accent);color:var(--accent-ink);text-decoration:none;display:inline-flex;align-items:center;gap:6px}
button.ghost,.btn.ghost{background:var(--bg2);color:var(--ink)}
button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid var(--accent-soft);outline-offset:2px}
input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px 12px;width:100%}
input::placeholder{color:var(--muted)}
footer.foot{color:var(--muted);font-size:13px;padding:28px 0 40px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.md h1{font-size:24px;margin:18px 0 10px}.md h2{font-size:20px;margin:22px 0 8px}.md h3{font-size:16px;margin:18px 0 6px}
.md p,.md ul,.md ol{margin:0 0 10px}.md li{margin:2px 0}.md code{font-family:var(--mono);font-size:12.5px;background:var(--bg2);padding:1px 5px;border-radius:6px}
.md pre,.yaml{font-family:var(--mono);font-size:12.5px;background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:14px;overflow:auto;line-height:1.5}
.md pre code{background:none;padding:0}.md blockquote{margin:0 0 10px;padding:6px 14px;border-left:3px solid var(--accent);color:var(--muted)}
.md table{border-collapse:collapse;margin:0 0 12px;font-size:13px;display:block;overflow:auto;max-width:100%;width:max-content}
.md th,.md td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;max-width:380px;overflow-wrap:break-word;min-width:70px}.md td:last-child{min-width:280px}.md th{background:var(--bg2)}
.yaml .k{color:var(--accent)}.yaml .c{color:var(--muted);font-style:italic}.yaml .s{color:var(--good)}
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def page(title: str, body: str, *, css: str = "", script: str = "", head: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">{head}
<style>{BASE_CSS}{css}</style></head><body>{body}{f"<script>{script}</script>" if script else ""}</body></html>"""


def nav(active: str, revision: str) -> str:
    links = [("home", "/", "Home"), ("status", "/status", "Status"), ("rules", "/status/rules", "Rules and code")]
    items = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>' for key, href, label in links)
    return (f'<nav class="nav wrap" aria-label="Main"><a class="brand" href="/"><span class="mark" aria-hidden="true">◐</span>'
            f'Vallum Links</a><div class="links">{items}</div></nav>')


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
