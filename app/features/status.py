"""Public status page (B27): a live lane for the release being test-driven, tiers and allowances, the release
queue, every feature as a card with its promises and their latest results, security and recent events, and a
read-only copy of the repo's rules and code map. Reads only public documents of the state store, written by code
in main pipelines (FR-STAT-2). Card: promises/features/base-status.yaml"""

import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.ui import esc, markdown, nav, page, yaml_html

router = APIRouter()

_cache = {"at": 0.0, "data": None}
_live_cache = {"at": 0.0, "data": None}
_docs_cache = {"at": 0.0, "data": None}
CACHE_SECONDS = 30
LIVE_SECONDS = 2
TIERS = ("core", "standard", "experimental", "app")
TIER_ORDER = {"core": 0, "standard": 1, "experimental": 2}
DOCS = ("release", "modes", "recent", "queue", "meta", "public", "security", "flags")
SHOWN_EVENTS = {"released", "rolled_back", "refused", "safe-mode", "live", "merged", "watchdog", "flag"}


def _load(store):
    if _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        data = {name: store.read_doc("vallum_state", name) or {} for name in DOCS}
    except Exception:  # the page must render even if the state store is unreachable
        data = {name: {} for name in DOCS} | {"error": True}
    _cache.update(at=time.monotonic(), data=data)
    return data


def _doc(store, name, cache, seconds):
    if cache["data"] is not None and time.monotonic() - cache["at"] < seconds:
        return cache["data"]
    try:
        data = store.read_doc("vallum_state", name) or {}
    except Exception:
        data = cache["data"] or {}
    cache.update(at=time.monotonic(), data=data)
    return data


def when(value: str | None) -> str:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    return t.astimezone(timezone.utc).strftime("%d %b %H:%M UTC")


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{esc(text)}</span>'


def _mode_tone(mode: str) -> str:
    return {"green": "good", "yellow": "warn", "red": "bad"}.get((mode or "green").lower(), "info")


# ---------------------------------------------------------------- tiers
def tiers_html(modes: dict, window: float | None) -> str:
    rows = []
    for t in TIERS:
        m = modes.get(t) or {}
        left = max(0.0, min(1.0, float(m.get("allowance", 1.0))))
        mode = (m.get("mode") or "green").lower()
        visits, bad, allowed = int(m.get("visits", 0)), int(m.get("bad", 0)), m.get("allowed_bad")
        span = f" in the last {window:g} min" if window else ""
        detail = (f"{bad} bad of {visits} visits; {round(float(allowed))} allowed{span}" if visits and allowed is not None
                  else f"no visits{span}")
        label = "Whole app" if t == "app" else t.capitalize()
        pct = round(100 * left)
        rows.append(f'<div class="tier"><div class="ring {_mode_tone(mode)}" style="--p:{pct}" role="img" '
                    f'aria-label="{esc(label)} allowance {pct}% left"><span><b>{pct}%</b><small>left</small></span></div>'
                    f'<div><div class="tname">{esc(label)} {_pill(mode.upper(), _mode_tone(mode))}</div>'
                    f'<div class="muted small">{pct}% left · {esc(detail)}</div></div></div>')
    return "".join(rows)


# ---------------------------------------------------------------- releases and the queue
def release_text(release: dict) -> str:
    slot = release.get("slot", "idle")
    rid, feature = release.get("release_id") or "", release.get("feature") or ""
    if slot == "driving":
        step = release.get("step")
        where = "step 0 on a private address" if not step else f"{step}% of visitors on the new version"
        return f"Test drive {esc(rid)} ({esc(feature)}): {where}."
    if slot == "merged":
        return f"{esc(rid)} ({esc(feature)}) is merged; its test drive starts next."
    if slot == "gated":
        return "The Merge Desk is checking the next change."
    last = f" Last release: {esc(rid)} ({esc(feature)}), {esc(when(release.get('released_at')))}." if rid else ""
    return "Idle." + last


def queue_html(queue: dict, project_url: str) -> str:
    entries = queue.get("entries")
    if entries is None:          # older Merge Desk runs stored plain lines
        items = queue.get("items") or []
        return ("<ol class='queue'>" + "".join(f"<li>{esc(i)}</li>" for i in items[:8]) + "</ol>") if items else \
            '<p class="muted small">Nothing waiting.</p>'
    if not entries:
        return '<p class="muted small">Nothing waiting.</p>'
    tones = {"P0": "bad", "P1": "warn", "P2": "info", "P3": "info"}
    rows = []
    for n, e in enumerate(entries[:8], 1):
        link = f'{esc(project_url)}/-/merge_requests/{int(e["iid"])}' if project_url else ""
        title = f'<a href="{link}">!{int(e["iid"])} {esc(e.get("title"))}</a>' if link else f'!{int(e["iid"])} {esc(e.get("title"))}'
        state = esc(e.get("state", "")) + (f" · {esc(e['why'])}" if e.get("why") else "")
        rows.append(f'<li><span class="qn">{n}</span>{_pill(e.get("priority") or "P2", tones.get(e.get("priority"), "info"))}'
                    f'<span class="qt">{title}<span class="muted small"> · {state}</span></span></li>')
    return '<ol class="queue">' + "".join(rows) + "</ol>"


def release_html(release: dict, meta: dict, queue: dict, breaker: dict, project_url: str = "") -> str:
    hours, limit = float(breaker.get("hours", 6)), int(breaker.get("limit", 3))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = 0
    for t in meta.get("rollbacks") or []:
        try:
            recent += datetime.fromisoformat(str(t).replace("Z", "+00:00")) > cutoff
        except ValueError:
            pass
    safe = meta.get("safe_mode")
    chips = (_pill("Safe mode ON", "bad") if safe else _pill("Safe mode off", "good")) + " " + \
        _pill(f"Circuit breaker {recent} of {limit} rollbacks in {hours:g} h", "warn" if recent else "info")
    reason = (f'<p class="note">Safe mode: {esc(meta.get("safe_mode_reason") or "on")}. No merges or releases until a '
              'member resumes.</p>') if safe else ""
    return (f'<p>{release_text(release)}</p><div class="chips">{chips}</div>{reason}'
            f'<h3 class="mini">Release queue <span class="muted small">(priority first, then age)</span></h3>'
            f'{queue_html(queue, project_url)}')


# ---------------------------------------------------------------- feature cards
def _result(p: dict, judged: dict, feature: str, checked_at: str) -> tuple[str, str]:
    """(tone, text) for one promise: the live check first, then the last test drive, then where it is checked."""
    if p.get("live") == "broken":
        return "bad", f"broken in production (live check, {when(checked_at)})"
    if p.get("live") == "switched off":
        return "warn", "not checked: the feature is switched off by its kill switch"
    if p.get("live") == "kept":
        return "good", f"kept in production (live check, {when(checked_at)})"
    j = judged.get(f"{feature}/{p.get('id')}")
    if j:
        step = j.get("step")
        at = "step 0" if not step else f"{step}%"
        verdict = str(j.get("verdict") or "").replace("_", " ")
        tone = "good" if verdict == "kept" else ("info" if verdict == "collecting" else "warn")
        detail = f": {j['detail']}" if j.get("detail") else ""
        return tone, f"{verdict} in test drive {j.get('release')} at {at}{detail}"
    if "mr" in (p.get("where") or []):
        # a feature merges only when every one of its mr promises is kept (Gatekeeper rule 7)
        return "good", "kept on its merge request (checked before every merge)"
    return "info", "checked when the feature changes"


def features_html(public: dict, switches: dict | None = None) -> str:
    features, switches = public.get("features") or {}, switches or {}
    if not features:
        return '<p class="muted">The Live Patrol publishes the live features after its next run.</p>'
    judged, checked_at, project_url = public.get("judged") or {}, public.get("checked_at"), public.get("project_url") or ""
    out = []
    for name, f in sorted(features.items(), key=lambda kv: (TIER_ORDER.get(kv[1].get("tier"), 9), kv[0])):
        promises = f.get("promises") or []
        results = [_result(p, judged, name, checked_at) for p in promises]
        broken = sum(1 for tone, _ in results if tone == "bad")
        kept = sum(1 for tone, _ in results if tone == "good")
        summary = f"{len(promises)} promises · {kept} kept" + (f" · {broken} broken" if broken else "")
        tier = f.get("tier") or "?"
        off = bool(f.get("flag")) and switches.get(f["flag"]) is False
        types = {}
        for p in promises:
            types[p.get("type")] = types.get(p.get("type"), 0) + 1
        chips = "".join(f'<span class="tchip">{esc(t)} {n}</span>' for t, n in sorted(types.items()))
        rows = "".join(
            f'<li><span class="dot {tone}" aria-hidden="true"></span><div><b>{esc(p.get("id"))}</b> '
            f'<span class="muted small">{esc(p.get("type"))} · {esc(p.get("severity"))}'
            f'{" · watch" if p.get("source") == "watch" else ""}{" · learned" if p.get("source") == "learned" else ""}</span>'
            f'<br>{esc(p.get("says"))}<br><span class="small {tone}-text">{esc(text)}</span></div></li>'
            for p, (tone, text) in zip(promises, results))
        switch = ""
        if f.get("flag"):
            switch = (f'<span class="switch {"off" if off else "on"}" title="kill switch {esc(f["flag"])}">'
                      f'<i aria-hidden="true"></i>{"switched off" if off else "switch on"}</span>')
        routes = "".join(f'<span class="route mono">{esc(r)}</span>' for r in f.get("routes") or [])
        issue = f.get("issue")
        origin = (f'<a href="{esc(project_url)}/-/work_items/{int(issue)}">from issue #{int(issue)}</a>'
                  if issue and project_url and str(issue).isdigit() else "base feature")
        card_file = f.get("path") or ""
        back = (f'<details class="back"><summary>Card file · <span class="mono">{esc(card_file)}</span></summary>'
                f'{yaml_html(f.get("yaml") or "")}</details>') if f.get("yaml") else ""
        score = "bad" if broken else ("good" if kept == len(promises) and promises else "info")
        out.append(
            f'<article class="fcard tier-{esc(tier)}{" broken" if broken else ""}{" off" if off else ""}">'
            f'<div class="band"><span>{esc(tier.capitalize())}</span><span>{esc(f.get("priority") or "")}</span>{switch}</div>'
            f'<div class="fbody"><h3>{esc(name)}</h3>'
            f'<div class="muted small">{esc(f.get("kind") or "")} · {origin}</div>'
            f'<div class="routes">{routes}</div>'
            f'<div class="score {score}"><b>{kept}/{len(promises)}</b> promises kept{f" · {broken} broken" if broken else ""}</div>'
            f'<div class="types">{chips}</div>'
            f'<details class="plist"{" open" if broken else ""}><summary>{esc(summary)}</summary><ul class="promises">{rows}</ul></details>'
            f'{back}</div></article>')
    return '<div class="cards">' + "".join(out) + "</div>"


# ---------------------------------------------------------------- security and events
def security_html(sec: dict) -> str:
    """Counts only: finding details stay in the project's vulnerability report and the Ledger."""
    if not sec.get("at"):
        return '<p class="muted">The Live Patrol triages scanner findings on its next run.</p>'
    counts, total = sec.get("counts") or {}, int(sec.get("open", 0))
    if not total:
        return f'<p>No open scanner findings on main. Checked {esc(when(sec["at"]))}.</p>'
    order = (("real", "bad"), ("accepted", "warn"), ("image", "info"), ("not shipped", "info"), ("fixed on main", "good"))
    chips = " ".join(_pill(f"{counts[k]} {k}", tone) for k, tone in order if counts.get(k))
    return (f'<p>{total} open finding{"s" if total != 1 else ""} from SAST, secret detection and container scanning, '
            f'each with a decision made by code ({esc(when(sec["at"]))}).</p><div class="chips">{chips}</div>'
            '<p class="muted small">Real high or critical findings in shipped code become fix issues that Vallum builds; '
            'new ones block merges.</p>')


def events_html(events: list[dict], project_url: str) -> str:
    shown = [e for e in events if e.get("kind") in SHOWN_EVENTS][:12]
    if not shown:
        return '<p class="muted">No releases yet.</p>'
    tones = {"released": "good", "merged": "info", "rolled_back": "bad", "refused": "warn", "safe-mode": "bad",
             "live": "bad", "watchdog": "warn", "flag": "warn"}
    rows = []
    for e in shown:
        link, incident = "", str(e.get("incident") or "")
        if incident.isdigit() and project_url:
            link = f' <a href="{esc(project_url)}/-/issues/{incident}">incident #{incident}</a>'
        rows.append(f'<li><span class="muted small when">{esc(when(e.get("at")))}</span>'
                    f'{_pill(str(e.get("kind")).replace("_", " "), tones.get(e.get("kind"), "info"))} '
                    f'<span>{esc(e.get("summary"))}{link}</span></li>')
    return '<ul class="events">' + "".join(rows) + "</ul>"


def overall(data: dict, live: dict) -> tuple[str, str]:
    public, meta = data["public"], data["meta"]
    if meta.get("safe_mode"):
        return "bad", "Safe mode: nothing ships until a member resumes"
    if live.get("phase") in ("starting", "step0", "traffic"):
        return "accent", f"Shipping {live.get('release_id') or ''} now"
    broken = any(p.get("live") == "broken" for f in (public.get("features") or {}).values() for p in f.get("promises") or [])
    if broken:
        return "bad", "A promise is broken in production"
    if any((data["modes"].get(t) or {}).get("mode") == "red" for t in TIERS):
        return "warn", "A tier is Red: risky changes are paused"
    return "good", "Every live promise is kept"


STATUS_CSS = """
.head{display:flex;gap:18px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;padding:6px 0 18px}
.head h1{font-size:clamp(28px,4vw,40px)}.head p{margin:6px 0 0;color:var(--muted);max-width:640px}
.big-pill{font-size:14px;padding:8px 14px}
section.block{margin:0 0 20px}.block>h2{font-size:20px;margin:0 0 12px;display:flex;gap:10px;align-items:baseline}
.panel{padding:20px}.cols{display:grid;grid-template-columns:1.1fr .9fr;gap:20px}@media (max-width:900px){.cols{grid-template-columns:1fr}}
.tiers{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:520px){.tiers{grid-template-columns:1fr}}
.tier{display:flex;gap:14px;align-items:center}.tname{font-weight:600;display:flex;gap:8px;align-items:center}
.ring{--c:var(--good);width:74px;height:74px;flex:none;border-radius:50%;background:conic-gradient(var(--c) calc(var(--p)*1%),var(--bg2) 0);display:grid;place-items:center}
.ring.warn{--c:var(--warn)}.ring.bad{--c:var(--bad)}.ring span{width:56px;height:56px;border-radius:50%;background:var(--card);display:grid;place-items:center;align-content:center;line-height:1.05}
.ring b{font-size:16px}.ring small{font-size:11px;color:var(--muted)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 10px}.note{color:var(--bad)}h3.mini{font-family:Inter,sans-serif;font-size:14px;margin:14px 0 6px}
ol.queue{list-style:none;margin:0;padding:0}ol.queue li{display:flex;gap:10px;align-items:center;padding:8px 0;border-top:1px dashed var(--line)}
ol.queue li:first-child{border-top:0}.qn{width:22px;height:22px;border-radius:50%;background:var(--bg2);display:grid;place-items:center;font-size:12px;font-weight:600;flex:none}
.qt{min-width:0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:18px}
.fcard{background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);overflow:hidden;transition:transform .15s ease,box-shadow .15s ease;position:relative}
.fcard:hover{transform:translateY(-2px)}
.fcard .band{display:flex;gap:8px;align-items:center;padding:9px 14px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#fff;background:var(--standard)}
.fcard.tier-core .band{background:var(--core)}.fcard.tier-experimental .band{background:var(--experimental)}
@media (prefers-color-scheme:dark){.fcard .band{color:#1b1815}}
.fcard.broken{border-color:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)}.fcard.off{opacity:.8}
.switch{margin-left:auto;display:inline-flex;gap:6px;align-items:center;text-transform:none;letter-spacing:0;font-weight:600}
.switch i{width:26px;height:15px;border-radius:999px;background:rgba(255,255,255,.45);position:relative}
.switch i::after{content:"";position:absolute;top:2px;left:13px;width:11px;height:11px;border-radius:50%;background:#fff}
.switch.off i::after{left:2px}
.fbody{padding:14px 16px 16px}.fbody h3{font-size:20px;margin:0 0 2px}
.routes{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.route{background:var(--bg2);border-radius:8px;padding:2px 7px}
.score{font-size:14px;margin:4px 0 8px}.score b{font-family:Fraunces,Georgia,serif;font-size:22px;margin-right:4px}
.score.good b{color:var(--good)}.score.bad b{color:var(--bad)}.score.info b{color:var(--info)}
.types{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.tchip{font-size:12px;padding:2px 8px;border-radius:8px;border:1px solid var(--line);color:var(--muted)}
details{border-top:1px solid var(--line);padding:8px 0 0;margin-top:8px}summary{cursor:pointer;font-weight:600;font-size:14px}
summary::marker{color:var(--muted)}
ul.promises{list-style:none;margin:8px 0 0;padding:0;max-height:380px;overflow:auto}
ul.promises li{display:flex;gap:10px;padding:7px 0;border-top:1px dashed var(--line);font-size:14px}ul.promises li:first-child{border-top:0}
.dot{flex:none;width:10px;height:10px;border-radius:50%;margin-top:6px}.dot.good{background:var(--good)}.dot.warn{background:var(--warn)}
.dot.bad{background:var(--bad)}.dot.info{background:var(--muted)}
.good-text{color:var(--good)}.warn-text{color:var(--warn)}.bad-text{color:var(--bad)}.info-text{color:var(--muted)}
details.back .yaml{max-height:320px;margin-top:8px}
ul.events{list-style:none;margin:0;padding:0}ul.events li{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;padding:8px 0;border-top:1px solid var(--line)}
ul.events li:first-child{border-top:0}.when{min-width:110px}
/* live lane */
.lane{padding:20px;position:relative;overflow:hidden}
.lane.active{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft),var(--shadow)}
.lane-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}.lane-head h3{font-size:20px}
.pulse::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;animation:pulse 1.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 currentColor}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
.track{display:flex;align-items:center;margin:4px 0 18px}.stop{display:flex;flex-direction:column;align-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--muted);min-width:56px}
.stop .c{width:26px;height:26px;border-radius:50%;border:2px solid var(--line);background:var(--card);display:grid;place-items:center;font-size:13px}
.stop.done .c{background:var(--good);border-color:var(--good);color:#fff}.stop.fail .c{background:var(--bad);border-color:var(--bad);color:#fff}
.stop.now .c{border-color:var(--accent);color:var(--accent);animation:pulse 1.4s infinite}.stop.now{color:var(--accent)}
.seg{flex:1;height:3px;background:var(--line);border-radius:3px;margin-bottom:22px}.seg.done{background:var(--good)}
.split{display:flex;height:34px;border-radius:12px;overflow:hidden;background:var(--bg2);font-size:13px;font-weight:600}
.split .new{background:var(--accent);color:var(--accent-ink);display:flex;align-items:center;padding:0 10px;white-space:nowrap;transition:width .8s ease;min-width:0}
.split .old{flex:1;display:flex;align-items:center;justify-content:flex-end;padding:0 10px;color:var(--muted);white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:14px}
.stat{background:var(--bg2);border-radius:14px;padding:10px 12px}.stat b{display:block;font-size:18px;font-family:Fraunces,Georgia,serif}
.timer{height:5px;background:var(--line);border-radius:5px;margin-top:6px;overflow:hidden}.timer i{display:block;height:100%;background:var(--accent);transition:width 1s linear}
.lane pre{font-family:var(--mono);font-size:12px;background:var(--bg2);border-radius:12px;padding:12px;max-height:260px;overflow:auto;white-space:pre-wrap;word-break:break-word}
"""

LANE_SCRIPT = r"""
const lane = document.getElementById('lane');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ago = iso => { if (!iso) return ''; const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000); return s < 90 ? Math.round(s) + ' s ago' : Math.round(s / 60) + ' min ago'; };
const ACTIVE = ['starting', 'step0', 'traffic'];
let logOpen = false;
function render(data) {
  const d = data.drive || {}, active = ACTIVE.includes(d.phase) && Date.now() - Date.parse(d.updated_at || 0) < 15 * 60e3;
  const recent = !active && d.finished_at && Date.now() - Date.parse(d.finished_at) < 3 * 3600e3;
  lane.className = 'card lane' + (active ? ' active' : '');
  if (!active && !recent) {
    const r = data.release || {};
    lane.innerHTML = '<div class="lane-head"><span class="pill info">No release in progress</span><h3>All quiet</h3></div>' +
      '<p class="muted">' + esc(r.release_id ? 'Last release: ' + r.release_id + ' (' + (r.feature || '') + ').' : 'Nothing has shipped yet.') +
      ' When a change merges, its test drive appears here live: step 0, then a growing share of visitors.</p>';
    return;
  }
  const steps = [0].concat(d.steps || []), verdicts = {};
  (d.verdicts || []).forEach(v => verdicts[v.step] = v.verdict);
  const pct = d.phase === 'released' ? 100 : (d.phase === 'traffic' ? d.pct : 0);
  const failed = ['rolled_back', 'refused'].includes(d.phase);
  let track = '';
  steps.forEach((s, i) => {
    const label = s === 0 ? 'Step 0' : s + '%';
    let cls = '';
    if (verdicts[s] === 'kept' || d.phase === 'released') cls = 'done';
    else if (verdicts[s] && verdicts[s] !== 'kept') cls = 'fail';
    else if (active && ((s === 0 && d.phase !== 'traffic') || (d.phase === 'traffic' && s === d.pct))) cls = 'now';
    else if (failed && ((s === 0 && d.phase === 'refused') || (d.phase === 'rolled_back' && s === d.pct))) cls = 'fail';
    if (i) track += '<span class="seg ' + (cls === 'done' ? 'done' : '') + '"></span>';
    track += '<span class="stop ' + cls + '"><span class="c">' + (cls === 'done' ? '✓' : cls === 'fail' ? '✕' : '') + '</span>' + label + '</span>';
  });
  const head = active ? '<span class="pill accent pulse">Shipping now</span>' :
    d.phase === 'released' ? '<span class="pill good">Released</span>' :
    '<span class="pill bad">' + esc(d.phase === 'refused' ? 'Refused at step 0' : 'Rolled back') + '</span>';
  const mr = d.mr_iid && data.project_url ? ' · <a href="' + esc(data.project_url) + '/-/merge_requests/' + Number(d.mr_iid) + '">!' + Number(d.mr_iid) + '</a>' : '';
  const where = d.phase === 'step0' || d.phase === 'starting' ? 'Step 0: the new version runs on a private address; no visitor sees it yet.' :
    d.phase === 'traffic' ? pct + '% of visitors are on the new version; the rest stay on the current one.' :
    d.phase === 'released' ? 'Every visitor is on the new version.' : esc(d.reason || '');
  const watch = Number(d.watch_s || 0), el = Number(d.elapsed || 0), tw = watch ? Math.min(100, 100 * el / watch) : 0;
  const stats = active ? '<div class="stats">' +
    '<div class="stat"><span class="muted small">This step</span><b>' + el + ' / ' + watch + ' s</b><div class="timer"><i style="width:' + tw + '%"></i></div></div>' +
    '<div class="stat"><span class="muted small">Visits (new version)</span><b>' + Number(d.visits || 0) + ' (' + Number(d.visits_new || 0) + ')</b></div>' +
    '<div class="stat"><span class="muted small">Judgment</span><b>' + esc(d.status || (d.phase === 'step0' ? 'checking' : 'starting')) + '</b></div>' +
    '<div class="stat"><span class="muted small">Closest promise</span><b class="small" style="font-family:inherit;font-size:13px">' + esc(d.worst || '—') + '</b></div></div>' : '';
  const log = (d.log || []).join('\n');
  lane.innerHTML = '<div class="lane-head">' + head + '<h3>' + esc(d.title || d.feature || '') + '</h3><span class="muted small">' +
    esc(d.release_id || '') + ' · ' + esc((d.tier || '').replace(/^./, c => c.toUpperCase())) + mr + ' · updated ' + ago(d.updated_at) + '</span></div>' +
    '<div class="track" aria-label="Test-drive steps">' + track + '</div>' +
    '<div class="split" role="img" aria-label="' + pct + '% of visitors on the new version"><div class="new" style="width:' + Math.max(pct, pct ? 8 : 0) + '%;min-width:' + (pct ? 72 : 0) + 'px">' + (pct ? 'new ' + pct + '%' : '') + '</div>' + (pct < 100 ? '<div class="old">current ' + (100 - pct) + '%</div>' : '') + '</div>' +
    '<p class="muted small" style="margin:10px 0 0">' + where + '</p>' + stats +
    '<details id="lane-log"' + (logOpen ? ' open' : '') + '><summary>Live log</summary><pre>' + esc(log || 'No log lines yet.') + '</pre></details>';
  document.getElementById('lane-log').ontoggle = e => { logOpen = e.target.open; };
}
async function poll() {
  let wait = 20000;
  try {
    const r = await fetch('/status/live.json', {cache: 'no-store'});
    if (r.ok) { const data = await r.json(); render(data); if (ACTIVE.includes((data.drive || {}).phase)) wait = 3000; }
  } catch (e) {}
  setTimeout(poll, wait);
}
render(JSON.parse(document.getElementById('live-data').textContent));
setTimeout(poll, 3000);
"""


def live_payload(store, data: dict) -> dict:
    drive = _doc(store, "drive_live", _live_cache, LIVE_SECONDS)
    release = data.get("release") or {}
    return {"drive": drive, "project_url": (data.get("public") or {}).get("project_url") or "",
            "release": {k: release.get(k) for k in ("slot", "release_id", "feature", "step", "released_at")}}


def render(data: dict, revision: str, live: dict | None = None) -> str:
    release, modes, meta, public = data["release"], data["modes"], data["meta"], data["public"]
    live = live or {"drive": {}, "release": {}, "project_url": public.get("project_url") or ""}
    project_url = public.get("project_url") or ""
    tone, headline = overall(data, live.get("drive") or {})
    checked = f" Live checks: {esc(when(public.get('checked_at')))}." if public.get("checked_at") else ""
    rolled = ('<p class="note">Production was rolled back on purpose to an earlier version; live checks of newer '
              'features wait for the next release.</p>' if public.get("rolled_back") else "")
    unreachable = '<p class="note">The state store could not be read just now; showing what is known.</p>' if data.get("error") else ""
    links = []
    if project_url:
        links.append(f'<a href="{esc(project_url)}">source and pipelines</a>')
    if public.get("ledger_url"):
        links.append(f'<a href="{esc(public["ledger_url"])}">the Ledger</a>')
    live_json = json.dumps(live, default=str).replace("</", "<\\/")
    body = f"""{nav("status", revision)}
<main class="wrap">
<div class="head"><div><h1>Vallum status</h1>
<p>Every change to this link shortener is shipped by Vallum: agents propose, code decides, and each release is
test-driven on real traffic before everyone gets it. Revision <span class="mono">{esc(revision)}</span>.{checked}</p>
<p class="small">Visitors are simulated by CI jobs during the hackathon.</p></div>
<span class="pill {tone} big-pill">{esc(headline)}</span></div>
{unreachable}{rolled}
<section class="block"><h2>Now shipping</h2>
<div id="lane" class="card lane"><p>{release_text(release)}</p></div></section>
<div class="cols">
<section class="block card panel"><h2>Mistake allowance</h2><div class="tiers">{tiers_html(modes, public.get("window_minutes"))}</div></section>
<section class="block card panel"><h2>Releases</h2>{release_html(release, meta, data["queue"], public.get("breaker") or {}, project_url)}</section>
</div>
<section class="block"><h2>Features and their promises <span class="muted small">every feature is a Feature Card in the repo</span></h2>
{features_html(public, (data.get("flags") or {}).get("flags"))}</section>
<div class="cols">
<section class="block card panel"><h2>Recent releases, rollbacks and incidents</h2>{events_html(data["recent"].get("events", []), project_url)}</section>
<section class="block card panel"><h2>Security</h2>{security_html(data.get("security") or {})}</section>
</div>
</main>
<footer class="foot wrap">{"See " + " and ".join(links) + ". " if links else ""}This page reads only public data and updates itself.</footer>
<script type="application/json" id="live-data">{live_json}</script>"""
    return page("Vallum Status", body, css=STATUS_CSS, script=LANE_SCRIPT)


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    store = request.app.state.store
    data = _load(store)
    return HTMLResponse(render(data, request.app.state.revision, live_payload(store, data)),
                        headers={"Cache-Control": "public, max-age=30"})


@router.get("/status/live.json")
def live_json(request: Request):
    store = request.app.state.store
    return JSONResponse(live_payload(store, _load(store)), headers={"Cache-Control": "no-store"})


RULES_CSS = """
.rules{display:grid;grid-template-columns:230px 1fr;gap:20px;align-items:start}@media (max-width:820px){.rules{grid-template-columns:1fr}}
.toc{padding:10px;position:sticky;top:12px}.toc a{display:block;padding:8px 12px;border-radius:10px;color:var(--ink);text-decoration:none;font-weight:500}
.toc a.on{background:var(--accent-soft);color:var(--accent)}.rules>div{min-width:0}.doc{padding:22px;min-width:0}.doc[hidden]{display:none}
.doc .meta{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:4px 0 14px}
.readonly{display:block;padding:12px 16px;margin:0 0 18px;border-radius:14px;background:var(--info-soft);color:var(--info);font-size:14px}
"""

RULES_SCRIPT = r"""
const links = [...document.querySelectorAll('.toc a')], docs = [...document.querySelectorAll('.doc')];
function show(id) { docs.forEach(d => d.hidden = d.id !== id); links.forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#' + id)); }
links.forEach(a => a.onclick = e => { e.preventDefault(); show(a.getAttribute('href').slice(1)); history.replaceState(null, '', a.getAttribute('href')); });
show((location.hash || '#' + (docs[0] ? docs[0].id : '')).slice(1));
"""


def render_rules(docs: dict, revision: str, project_url: str = "") -> str:
    files = docs.get("files") or []
    commit = (docs.get("commit") or "")[:8]
    if not files:
        content = '<p class="muted">The Live Patrol publishes a copy of the rules and the code map on its next run.</p>'
        toc = ""
    else:
        toc, parts = [], []
        for n, f in enumerate(files):
            did = f"doc-{n}"
            path = f.get("path") or ""
            base = f"{project_url}/-/blob/main/{path.rsplit('/', 1)[0]}" if project_url and "/" in path else (
                f"{project_url}/-/blob/main" if project_url else "")
            text = f.get("text") or ""
            rendered = markdown(text, base) if f.get("format") == "md" else yaml_html(text)
            open_link = (f'<a href="{esc(project_url)}/-/blob/main/{esc(path)}" rel="noopener">Open on GitLab</a>'
                         if project_url else "")
            toc.append(f'<a href="#{did}">{esc(f.get("title"))}</a>')
            parts.append(f'<article class="card doc" id="{did}"><h2>{esc(f.get("title"))}</h2>'
                         f'<div class="meta"><span class="mono muted">{esc(path)}</span>{open_link}</div>{rendered}</article>')
        toc = '<nav class="card toc" aria-label="Documents">' + "".join(toc) + "</nav>"
        content = "".join(parts)
    at = f" at {esc(commit)}" if commit else ""
    refreshed = f" ({esc(when(docs.get('at')))})" if docs.get("at") else ""
    note = (f'<div class="readonly">🔒 Read-only copy of <b>main</b>{at}, refreshed by every Live Patrol{refreshed}. '
            'These files change only through a merge request; agents can never change the Rulebook, the policy or '
            'the code map.</div>')
    body = f"""{nav("rules", revision)}
<main class="wrap"><div class="head"><div><h1>Rules and code</h1>
<p>What every agent reads before it works, and what code enforces: the Rulebook, the policy, the catalog of
allowed changes, the coding guidelines, the code map and the project profile.</p></div></div>
{note}<div class="rules">{toc}<div>{content}</div></div></main>
<footer class="foot wrap">Visitors are simulated by CI jobs during the hackathon.</footer>"""
    return page("Vallum Rules and code", body, css=STATUS_CSS + RULES_CSS, script=RULES_SCRIPT if files else "")


@router.get("/status/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    store = request.app.state.store
    docs = _doc(store, "docs", _docs_cache, 60)
    project_url = (_load(store).get("public") or {}).get("project_url") or ""
    return HTMLResponse(render_rules(docs, request.app.state.revision, project_url),
                        headers={"Cache-Control": "public, max-age=60"})
