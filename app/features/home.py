"""The home page: shorten a link, check a code, see the links you made. Card: promises/features/base-home.yaml
The words visitors read first live in HEADLINE and TAGLINE; a feature request can change them like any change."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import flags
from app.ui import CONTROL_ROOM, esc, header, page

router = APIRouter()

SOURCE = "https://gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43503198/vallum"
HEADLINE = "Short links, shipped with care."
TAGLINE = ("Paste a long link and get a short one you can share anywhere. Every change to this little app is "
           "test-driven by Vallum before it reaches you.")

CSS = """
.hero{text-align:center;padding:58px 0 10px}@media (max-width:560px){.hero{padding-top:36px}.tag .lead-in{display:none}.tag{padding:5px}.tile{min-height:0}}
.tag{display:inline-flex;gap:8px;align-items:center;font-size:13.5px;font-weight:500;padding:6px 8px 6px 14px;border-radius:999px;background:var(--card);border:1px solid var(--line);color:var(--ink2)}
.tag b{background:var(--lime);border-radius:999px;padding:1px 9px;font-weight:600;color:var(--ink)}
.hero h1{font-size:clamp(40px,6.8vw,78px);font-weight:800;max-width:920px;margin:24px auto 18px;letter-spacing:-.05em;line-height:.98}
.hero .lead{font-size:18.5px;color:var(--muted);max-width:640px;margin:0 auto}
.shorten{max-width:760px;margin:36px auto 0;background:var(--card);border:1px solid var(--line);border-radius:30px;padding:22px 22px 18px;box-shadow:var(--shadow);text-align:left}
.shorten h2{font-size:21px;font-weight:700;margin:0 0 12px 8px}
.shortbar{display:flex;gap:8px;background:var(--soft2);border:1px solid var(--line);border-radius:999px;padding:6px}
.shortbar input{border:0;background:transparent;padding:10px 14px}.shortbar input:focus-visible{outline:none}
.shortbar:focus-within{border-color:#b9d64a;box-shadow:0 0 0 4px var(--lime-soft)}
@media (max-width:560px){.shortbar{flex-direction:column;border-radius:22px}.shortbar button{width:100%}}
.result{display:none;margin-top:14px;padding:16px 18px;border-radius:20px;background:var(--lime-soft)}
.result.show{display:block}.result.err{background:var(--bad-soft)}
.short{font-family:var(--mono);font-size:16px;font-weight:600;word-break:break-all}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.actions button,.actions .btn{padding:7px 14px;font-size:13.5px}
details.key{margin:12px 8px 0;font-size:14px}details.key summary{cursor:pointer;color:var(--muted);font-weight:500;width:max-content}
details.key .row{display:flex;gap:8px;margin-top:10px}@media (max-width:560px){details.key .row{flex-direction:column}}
details.key p{margin:8px 0 0}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:48px}@media (max-width:920px){.tiles{grid-template-columns:1fr}}
.tile{background:var(--soft);border-radius:28px;padding:26px 24px 24px;display:flex;flex-direction:column;min-height:350px;color:var(--ink);text-decoration:none}
a.tile{transition:transform .18s ease}a.tile:hover{transform:translateY(-3px)}
.tile h2{font-size:26px;font-weight:700}.tile .inner{flex:1;display:flex;flex-direction:column;justify-content:center;padding:20px 0}
.tile p{margin:0;color:var(--ink2);font-size:14.5px}.tile .go{margin-top:12px;font-weight:600;font-size:14px}
.panelw{background:var(--card);border-radius:20px;padding:14px;box-shadow:0 1px 2px rgba(45,35,15,.05),0 8px 20px rgba(45,35,15,.05)}
.check{display:flex;gap:8px}.check input{padding:10px 14px}.check button{padding:10px 16px}
.panelw .result{margin-top:10px;padding:12px 14px;font-size:14.5px}
.recent{list-style:none;margin:0;padding:0;max-height:220px;overflow:auto}
.recent li{display:flex;gap:10px;align-items:center;padding:9px 4px;border-top:1px dashed var(--line);font-size:14px}.recent li:first-child{border-top:0}
.recent .code{font-family:var(--mono);font-weight:600}.recent .dest{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}
.recent button{padding:5px 11px;font-size:12.5px}.recent .none{color:var(--muted);display:block;text-align:center;padding:16px 8px;border:0}
.ladder{display:flex;flex-direction:column;gap:9px}
.ladder span{display:flex;align-items:center;gap:10px;background:var(--card);border-radius:999px;padding:9px 14px;font-size:14px;font-weight:600;box-shadow:0 1px 2px rgba(45,35,15,.06)}
.ladder i{font-style:normal;width:22px;height:22px;border-radius:50%;background:var(--good-soft);color:var(--good);display:grid;place-items:center;font-size:12px;flex:none}
.ladder span{white-space:nowrap}.ladder span:nth-child(1){width:max(58%,max-content)}.ladder span:nth-child(2){width:max(70%,max-content)}.ladder span:nth-child(3){width:max(84%,max-content)}
.ladder span.on{background:var(--lime)}.ladder span.on i{background:var(--dark);color:var(--lime)}
.badge{display:inline-flex;gap:8px;align-items:center;font-size:13.5px;padding:7px 14px;border-radius:999px;background:var(--card);border:1px solid var(--line);color:var(--ink2)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 0 3px var(--good-soft);flex:none}
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const store = {get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
               set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }};
function show(box, ok, nodes) { box.className = 'result show' + (ok ? '' : ' err'); box.replaceChildren(...nodes); }
function el(tag, attrs, text) { const n = document.createElement(tag); Object.assign(n, attrs || {}); if (text !== undefined) n.textContent = text; return n; }
function copyButton(link, label) {
  const b = el('button', {type: 'button', className: 'ghost'}, label || 'Copy');
  b.onclick = async () => { try { await navigator.clipboard.writeText(link.short_url); b.textContent = 'Copied'; } catch (e) { b.textContent = 'Select and copy'; } };
  return b;
}
function linkActions(link) {
  const wrap = el('div', {className: 'actions'});
  wrap.append(copyButton(link), el('a', {className: 'btn ghost', href: '/p/' + encodeURIComponent(link.code)}, 'Preview'),
              el('a', {className: 'btn ghost', href: link.short_url, target: '_blank', rel: 'noopener'}, 'Open'));
  return wrap;
}
function renderRecent() {
  const list = $('#recent'), items = store.get('vallum.recent', []);
  list.replaceChildren();
  if (!items.length) { list.append(el('li', {className: 'none'}, 'Nothing yet. Links you make here appear in this list.')); return; }
  for (const link of items) {
    const li = el('li'); li.append(el('span', {className: 'code'}, '/r/' + link.code), el('span', {className: 'dest', title: link.url}, link.url), copyButton(link));
    list.append(li);
  }
}
$('#key-save').onclick = () => { const v = $('#key').value.trim(); store.set('vallum.ownerKey', v || null); $('#key').value = ''; $('#key-state').textContent = v ? 'Owner key saved in this browser.' : 'No owner key saved.'; };
$('#key-forget').onclick = () => { store.set('vallum.ownerKey', null); $('#key-state').textContent = 'Owner key forgotten.'; };
$('#key-state').textContent = store.get('vallum.ownerKey', null) ? 'Owner key saved in this browser.' : 'No owner key saved yet.';
$('#shorten').onsubmit = async ev => {
  ev.preventDefault();
  const box = $('#result'), url = $('#url').value.trim(), key = store.get('vallum.ownerKey', null);
  if (!key) { $('#keybox').open = true; show(box, false, [el('div', {}, 'Creating links needs the owner key. Paste it below; it stays in this browser.')]); return; }
  const btn = $('#shorten button'); btn.disabled = true; btn.textContent = 'Shortening…';
  try {
    const r = await fetch('/api/links', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-API-Key': key}, body: JSON.stringify({url})});
    if (r.status === 201) {
      const link = await r.json();
      store.set('vallum.recent', [link, ...store.get('vallum.recent', []).filter(x => x.code !== link.code)].slice(0, 8));
      show(box, true, [el('div', {className: 'small muted'}, 'Your short link'), el('div', {className: 'short'}, link.short_url), linkActions(link)]);
      $('#url').value = ''; renderRecent();
    } else if (r.status === 401 || r.status === 503) {
      $('#keybox').open = true; show(box, false, [el('div', {}, 'The owner key is missing or wrong.')]);
    } else if (r.status === 422) {
      show(box, false, [el('div', {}, "That doesn't look like a web address. Start it with http:// or https://.")]);
    } else { show(box, false, [el('div', {}, 'Something went wrong (' + r.status + '). Please try again.')]); }
  } catch (e) { show(box, false, [el('div', {}, 'Could not reach the app. Check your connection.')]); }
  btn.disabled = false; btn.textContent = 'Shorten →';
};
$('#check').onsubmit = async ev => {
  ev.preventDefault();
  const box = $('#check-result'), code = $('#code').value.trim().replace(/^.*\/r\//, '');
  if (!code) return;
  try {
    const r = await fetch('/api/links/' + encodeURIComponent(code) + '/exists');
    const d = r.ok ? await r.json() : null;
    if (d && d.exists) show(box, true, [el('div', {}, 'Yes, /r/' + d.code + ' exists.')]);
    else if (d) show(box, false, [el('div', {}, 'No short link uses the code "' + d.code + '".')]);
    else show(box, false, [el('div', {}, 'The checker is switched off right now. Try again later.')]);
  } catch (e) { show(box, false, [el('div', {}, 'Could not reach the app.')]); }
};
renderRecent();
"""


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(flags.require("home"))])
def home(request: Request):
    revision = request.app.state.revision
    body = f"""{header()}
<main class="wrap">
  <section class="hero">
    <span class="tag"><span class="lead-in">A tiny link shortener</span> <b>shipped by agents, guarded by code</b></span>
    <h1>{esc(HEADLINE)}</h1>
    <p class="lead">{esc(TAGLINE)}</p>
    <div class="shorten">
      <h2>Shorten a link</h2>
      <form id="shorten" class="shortbar"><input id="url" type="url" required placeholder="Paste a long link, like https://example.com/a/very/long/path" aria-label="Long link"><button type="submit">Shorten →</button></form>
      <div id="result" class="result" role="status" aria-live="polite"></div>
      <details class="key" id="keybox"><summary>🔑 Owner key</summary>
        <div class="row"><input id="key" type="password" autocomplete="off" placeholder="Paste the owner key" aria-label="Owner key"><button id="key-save" type="button">Save</button><button id="key-forget" type="button" class="ghost">Forget</button></div>
        <p class="muted small" id="key-state"></p>
        <p class="muted small">Creating links needs the owner key. It is kept only in this browser and sent only to this app.</p>
      </details>
    </div>
  </section>
  <section class="tiles">
    <div class="tile"><h2>Check a code.</h2>
      <div class="inner"><div class="panelw"><form id="check" class="check"><input id="code" required placeholder="a code or a short link" aria-label="Code to check"><button type="submit">Check</button></form>
      <div id="check-result" class="result" role="status" aria-live="polite"></div></div></div>
      <p>See whether a short link exists. You'll never see where it goes.</p></div>
    <div class="tile"><h2>Your recent links.</h2>
      <div class="inner"><div class="panelw"><ul id="recent" class="recent"></ul></div></div>
      <p>The last eight you made, kept only in this browser.</p></div>
    <a class="tile" href="{CONTROL_ROOM}/releases" rel="noopener"><h2>Shipped with care.</h2>
      <div class="inner"><div class="ladder" aria-label="How a new version reaches visitors"><span><i>✓</i>Step 0 · private check</span><span><i>✓</i>10% of visitors</span><span><i>✓</i>50% of visitors</span><span class="on"><i>✓</i>100% · you</span></div></div>
      <p>Every new version meets a small share of visitors first. If it breaks a promise, it's rolled back in seconds.</p>
      <span class="go">Watch a release live ↗</span></a>
  </section>
</main>
<footer class="foot"><span class="badge"><span class="dot" aria-hidden="true"></span>Served by version <span class="mono">{esc(revision)}</span></span>
<span>Visitors you see in the numbers are simulated during the hackathon.</span>
<a href="{SOURCE}" rel="noopener">Source and every pipeline on GitLab</a></footer>"""
    return HTMLResponse(page("Vallum Links", body, css=CSS, script=SCRIPT),
                        headers={"Cache-Control": "no-store"})
