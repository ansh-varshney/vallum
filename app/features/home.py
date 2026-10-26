"""The home page: shorten a link, check a code, see the links you made. Card: promises/features/base-home.yaml
The words visitors read first live in HEADLINE and TAGLINE; a feature request can change them like any change."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import flags
from app.ui import esc, nav, page

router = APIRouter()

HEADLINE = "Short links, shipped with care."
TAGLINE = ("Paste a long link and get a short one you can share anywhere. Every change to this little app is "
           "test-driven by Vallum before it reaches you.")

CSS = """
.hero{display:grid;grid-template-columns:1.15fr .85fr;gap:22px;align-items:start;padding:18px 0 8px}
@media (max-width:860px){.hero{grid-template-columns:1fr}}
.hero h1{font-size:clamp(32px,5vw,50px);line-height:1.08;margin:6px 0 12px}
.hero p.lead{font-size:17px;color:var(--muted);max-width:560px;margin:0 0 18px}
.panel{padding:22px}.panel h2{font-size:20px;margin-bottom:4px}.panel .sub{color:var(--muted);font-size:14px;margin:0 0 14px}
.row{display:flex;gap:10px}.row input{flex:1}@media (max-width:520px){.row{flex-direction:column}}
.result{margin-top:14px;padding:14px;border-radius:14px;background:var(--good-soft);display:none}
.result.show{display:block}.result.err{background:var(--bad-soft)}
.short{font-family:var(--mono);font-size:15px;word-break:break-all}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.actions .btn,.actions button{padding:7px 12px;font-size:13px}
details.key{margin-top:12px;font-size:14px}details.key summary{cursor:pointer;color:var(--muted)}
details.key .row{margin-top:8px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:22px}@media (max-width:860px){.grid2{grid-template-columns:1fr}}
.recent{list-style:none;padding:0;margin:0}.recent li{display:flex;gap:10px;align-items:center;padding:10px 0;border-top:1px dashed var(--line)}
.recent li:first-child{border-top:0}.recent .dest{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}
.steps{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px}.steps span{font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;background:var(--bg2);color:var(--muted)}
.steps span.on{background:var(--accent-soft);color:var(--accent)}
.badge{display:inline-flex;gap:6px;align-items:center;font-size:13px;padding:6px 12px;border-radius:999px;background:var(--card);border:1px solid var(--line)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 0 3px var(--good-soft)}
.cozy{background:linear-gradient(135deg,var(--accent-soft),var(--bg2));border:0}
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const store = {get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
               set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }};
function show(box, ok, nodes) { box.className = 'result show' + (ok ? '' : ' err'); box.replaceChildren(...nodes); }
function el(tag, attrs, text) { const n = document.createElement(tag); Object.assign(n, attrs || {}); if (text !== undefined) n.textContent = text; return n; }
function linkActions(link) {
  const wrap = el('div', {className: 'actions'});
  const copy = el('button', {type: 'button', className: 'ghost'}, 'Copy');
  copy.onclick = async () => { try { await navigator.clipboard.writeText(link.short_url); copy.textContent = 'Copied'; } catch (e) { copy.textContent = 'Select and copy'; } };
  wrap.append(copy, el('a', {className: 'btn ghost', href: '/p/' + encodeURIComponent(link.code)}, 'Preview'),
              el('a', {className: 'btn ghost', href: link.short_url, target: '_blank', rel: 'noopener'}, 'Open'));
  return wrap;
}
function renderRecent() {
  const list = $('#recent'), items = store.get('vallum.recent', []);
  list.replaceChildren();
  if (!items.length) { list.append(el('li', {className: 'muted small'}, 'Links you make here appear in this list (kept only in this browser).')); return; }
  for (const link of items) {
    const li = el('li'); li.append(el('span', {className: 'short'}, '/r/' + link.code), el('span', {className: 'dest', title: link.url}, link.url), linkActions(link));
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
  btn.disabled = false; btn.textContent = 'Shorten';
};
$('#check').onsubmit = async ev => {
  ev.preventDefault();
  const box = $('#check-result'), code = $('#code').value.trim().replace(/^.*\/r\//, '');
  if (!code) return;
  try {
    const r = await fetch('/api/links/' + encodeURIComponent(code) + '/exists');
    const d = r.ok ? await r.json() : null;
    if (d && d.exists) show(box, true, [el('div', {}, 'Yes, /r/' + d.code + ' exists.'), linkActions({code: d.code, short_url: location.origin + '/r/' + d.code})]);
    else if (d) show(box, false, [el('div', {}, 'No short link uses the code "' + d.code + '".')]);
    else show(box, false, [el('div', {}, 'The checker is switched off right now. Try again later.')]);
  } catch (e) { show(box, false, [el('div', {}, 'Could not reach the app.')]); }
};
renderRecent();
"""


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(flags.require("home"))])
def home(request: Request):
    revision = request.app.state.revision
    body = f"""{nav("home", revision)}
<main class="wrap">
  <section class="hero">
    <div>
      <span class="pill accent">A tiny link shortener · shipped by agents, guarded by code</span>
      <h1>{esc(HEADLINE)}</h1>
      <p class="lead">{esc(TAGLINE)}</p>
      <div class="steps" aria-label="How a change reaches you"><span>step 0 · private</span><span>1–10% of visitors</span><span>50%</span><span class="on">100% · you</span></div>
      <p class="muted small">Every new version meets a small share of visitors first. If it breaks a promise, it's rolled back in seconds.</p>
    </div>
    <div class="card panel">
      <h2>Shorten a link</h2>
      <p class="sub">Paste a long web address.</p>
      <form id="shorten" class="row"><input id="url" type="url" required placeholder="https://example.com/a/very/long/path" aria-label="Long link"><button type="submit">Shorten</button></form>
      <div id="result" class="result" role="status" aria-live="polite"></div>
      <details class="key" id="keybox"><summary>🔑 Owner key</summary>
        <div class="row"><input id="key" type="password" autocomplete="off" placeholder="Paste the owner key" aria-label="Owner key"><button id="key-save" type="button" class="ghost">Save</button><button id="key-forget" type="button" class="ghost">Forget</button></div>
        <p class="muted small" id="key-state"></p>
        <p class="muted small">Creating links needs the owner key. It is kept only in this browser and sent only to this app.</p>
      </details>
    </div>
  </section>
  <section class="grid2">
    <div class="card panel">
      <h2>Check a code</h2>
      <p class="sub">Does a short link exist? You'll only see whether it does, not where it goes.</p>
      <form id="check" class="row"><input id="code" required placeholder="a code, or a whole short link" aria-label="Code to check"><button type="submit" class="ghost">Check</button></form>
      <div id="check-result" class="result" role="status" aria-live="polite"></div>
    </div>
    <div class="card panel">
      <h2>Your recent links</h2>
      <p class="sub">The last eight you made in this browser.</p>
      <ul id="recent" class="recent"></ul>
    </div>
  </section>
  <section class="card panel cozy" style="margin-top:22px">
    <h2>Curious how this ships?</h2>
    <p class="sub" style="margin:4px 0 12px">The status page shows every feature's promises, the mistake allowance and any release in progress, live.</p>
    <a class="btn" href="/status">Open the status page</a>
  </section>
</main>
<footer class="foot wrap"><span class="badge"><span class="dot" aria-hidden="true"></span>Served by version <span class="mono">{esc(revision)}</span></span>
<span>Visitors you see in the numbers are simulated during the hackathon.</span></footer>"""
    return HTMLResponse(page("Vallum Links", body, css=CSS, script=SCRIPT),
                        headers={"Cache-Control": "no-store"})
