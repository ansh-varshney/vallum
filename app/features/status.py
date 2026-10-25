"""Public status page (B27): tiers and allowances, the release in progress and the queue, every feature's
promises with their latest result, and recent releases and rollbacks. Reads only public documents of the
state store, written by code in main pipelines (FR-STAT-2). Card: promises/features/base-status.yaml"""

import html
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

_cache = {"at": 0.0, "data": None}
CACHE_SECONDS = 30
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


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


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
        label = "whole app" if t == "app" else t.capitalize()
        rows.append(f'<div class="tier"><div class="row"><b>{esc(label)}</b>{_pill(mode.upper(), _mode_tone(mode))}'
                    f'<span class="muted right">{round(100 * left)}% left</span></div>'
                    f'<div class="bar" role="img" aria-label="{esc(label)} allowance {round(100 * left)}% left">'
                    f'<span class="{_mode_tone(mode)}" style="width:{round(100 * left)}%"></span></div>'
                    f'<div class="muted small">{esc(detail)}</div></div>')
    return "".join(rows)


def release_html(release: dict, meta: dict, queue: dict, breaker: dict) -> str:
    slot = release.get("slot", "idle")
    rid, feature = release.get("release_id") or "", release.get("feature") or ""
    if slot == "driving":
        step = release.get("step")
        where = "step 0 on a private address" if not step else f"{step}% of visitors on the new version"
        now = f"Test drive {esc(rid)} ({esc(feature)}): {where}."
    elif slot == "merged":
        now = f"{esc(rid)} ({esc(feature)}) is merged; its test drive starts next."
    elif slot == "gated":
        now = "The Merge Desk is checking the next change."
    else:
        last = f" Last release: {esc(rid)} ({esc(feature)}), {esc(when(release.get('released_at')))}." if rid else ""
        now = "Idle." + last
    hours, limit = float(breaker.get("hours", 6)), int(breaker.get("limit", 3))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = 0
    for t in meta.get("rollbacks") or []:
        try:
            recent += datetime.fromisoformat(str(t).replace("Z", "+00:00")) > cutoff
        except ValueError:
            pass
    safe = meta.get("safe_mode")
    chips = (_pill("Safe mode ON", "bad") if safe else _pill("Safe mode off", "good")) + \
        _pill(f"Circuit breaker {recent} of {limit} rollbacks in {hours:g} h", "warn" if recent else "info")
    reason = f'<p class="note">Safe mode: {esc(meta.get("safe_mode_reason") or "on")}. No merges or releases until a member resumes.</p>' if safe else ""
    items = queue.get("items") or []
    q = ("<ol>" + "".join(f"<li>{esc(i)}</li>" for i in items[:8]) + "</ol>") if items else '<p class="muted">Nothing waiting.</p>'
    return f'<p>{now}</p><div class="chips">{chips}</div>{reason}<h3>Release queue</h3>{q}'


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
    where = p.get("where") or []
    if "mr" in where:
        return "info", "checked on every merge request before it can merge"
    return "info", "checked when the feature changes"


def features_html(public: dict, switches: dict | None = None) -> str:
    features, switches = public.get("features") or {}, switches or {}
    if not features:
        return '<p class="muted">The Live Patrol publishes the live features after its next run.</p>'
    judged, checked_at = public.get("judged") or {}, public.get("checked_at")
    out = []
    for name, f in sorted(features.items(), key=lambda kv: (TIER_ORDER.get(kv[1].get("tier"), 9), kv[0])):
        promises = f.get("promises") or []
        results = [_result(p, judged, name, checked_at) for p in promises]
        broken = sum(1 for tone, _ in results if tone == "bad")
        kept = sum(1 for tone, _ in results if tone == "good")
        summary = f"{len(promises)} promises · {kept} kept" + (f" · {broken} broken" if broken else "")
        rows = "".join(
            f'<li><span class="dot {tone}" aria-hidden="true"></span><div><b>{esc(p.get("id"))}</b> '
            f'<span class="muted">{esc(p.get("type"))} · {esc(p.get("severity"))}</span><br>{esc(p.get("says"))}'
            f'<br><span class="small {tone}-text">{esc(text)}</span></div></li>'
            for p, (tone, text) in zip(promises, results))
        tier = f.get("tier") or "?"
        off = bool(f.get("flag")) and switches.get(f["flag"]) is False
        flag = (f' · kill switch <code>{esc(f["flag"])}</code> {"OFF" if off else "on"}' if f.get("flag") else "")
        routes = ", ".join(f.get("routes") or [])
        out.append(f'<details{" open" if broken else ""}><summary><b>{esc(name)}</b> '
                   f'{_pill(tier.capitalize(), "info")} {_pill(f.get("priority") or "", "info")}'
                   f'{_pill("switched off", "warn") if off else ""}'
                   f'<span class="muted right">{esc(summary)}</span></summary>'
                   f'<p class="muted small">{esc(f.get("kind") or "")}{" · " + esc(routes) if routes else ""}{flag}</p>'
                   f'<ul class="promises">{rows}</ul></details>')
    return "".join(out)


def security_html(sec: dict) -> str:
    """Counts only: finding details stay in the project's vulnerability report and the Ledger."""
    if not sec.get("at"):
        return '<p class="muted">The Live Patrol triages scanner findings on its next run.</p>'
    counts, total = sec.get("counts") or {}, int(sec.get("open", 0))
    if not total:
        return f'<p>No open scanner findings on main. Checked {esc(when(sec["at"]))}.</p>'
    order = (("real", "bad"), ("accepted", "warn"), ("image", "info"), ("not shipped", "info"), ("fixed on main", "good"))
    chips = "".join(_pill(f"{counts[k]} {k}", tone) for k, tone in order if counts.get(k))
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
        rows.append(f'<li><span class="muted small">{esc(when(e.get("at")))}</span> '
                    f'{_pill(str(e.get("kind")).replace("_", " "), tones.get(e.get("kind"), "info"))} '
                    f'{esc(e.get("summary"))}{link}</li>')
    return '<ul class="events">' + "".join(rows) + "</ul>"


STYLE = """
:root{--bg:#f6f7fb;--fg:#1d2540;--muted:#5d6886;--card:#fff;--line:#dde2ee;--accent:#4f46e5;
--good:#1e7f58;--good-bg:#e3f4ec;--warn:#9a6700;--warn-bg:#fdf3d7;--bad:#b42318;--bad-bg:#fde7e4;--info:#3f4a6b;--info-bg:#eef0f7}
@media (prefers-color-scheme:dark){:root{--bg:#141a2b;--fg:#e8ecf7;--muted:#a3aecb;--card:#1d2440;--line:#323b5c;
--accent:#a5b4fc;--good:#6ee7b7;--good-bg:#173d33;--warn:#fcd34d;--warn-bg:#41361a;--bad:#fca5a5;--bad-bg:#4a2020;
--info:#c7d2fe;--info-bg:#2a3256}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 40px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 10px;color:var(--accent);text-transform:uppercase;letter-spacing:.04em}
h3{font-size:14px;margin:14px 0 6px}p{margin:0 0 10px}.muted{color:var(--muted)}.small{font-size:13px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.row{display:flex;align-items:center;gap:8px}.right{margin-left:auto}
.bar{height:8px;background:var(--info-bg);border-radius:6px;overflow:hidden;margin:6px 0 4px}
.bar span{display:block;height:100%}.bar .good{background:var(--good)}.bar .warn{background:var(--warn)}.bar .bad{background:var(--bad)}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;margin-right:4px;white-space:nowrap}
.pill.good{color:var(--good);background:var(--good-bg)}.pill.warn{color:var(--warn);background:var(--warn-bg)}
.pill.bad{color:var(--bad);background:var(--bad-bg)}.pill.info{color:var(--info);background:var(--info-bg)}
.chips{margin:4px 0 8px}.note{color:var(--bad)}
details{border-top:1px solid var(--line);padding:10px 0}details:first-of-type{border-top:0}
summary{cursor:pointer;display:flex;align-items:center;gap:6px;flex-wrap:wrap;list-style:none}
summary::-webkit-details-marker{display:none}summary::before{content:"▸";color:var(--muted);transition:transform .15s}
details[open]>summary::before{transform:rotate(90deg)}
ul.promises,ul.events{list-style:none;padding:0;margin:8px 0 0}ul.promises li{display:flex;gap:10px;padding:6px 0;border-top:1px dashed var(--line)}
.dot{flex:none;width:10px;height:10px;border-radius:50%;margin-top:6px}.dot.good{background:var(--good)}
.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}.dot.info{background:var(--muted)}
.good-text{color:var(--good)}.warn-text{color:var(--warn)}.bad-text{color:var(--bad)}.info-text{color:var(--muted)}
ul.events li{padding:6px 0;border-top:1px solid var(--line)}ul.events li:first-child{border-top:0}ol{margin:0;padding-left:20px}
a{color:var(--accent)}code{font-size:13px}footer{color:var(--muted);font-size:13px;margin-top:8px}
"""


def render(data: dict, revision: str) -> str:
    release, modes, meta, public = data["release"], data["modes"], data["meta"], data["public"]
    project_url = public.get("project_url") or ""
    links = []
    if project_url:
        links.append(f'<a href="{esc(project_url)}">source and pipelines</a>')
    if public.get("ledger_url"):
        links.append(f'<a href="{esc(public["ledger_url"])}">the Ledger</a>')
    checked = f" Live checks: {esc(when(public.get('checked_at')))}." if public.get("checked_at") else ""
    rolled = ('<p class="note">Production was rolled back on purpose to an earlier version; live checks of newer '
              'features wait for the next release.</p>' if public.get("rolled_back") else "")
    unreachable = '<p class="note">The state store could not be read just now; showing what is known.</p>' if data.get("error") else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Vallum Status</title>
<style>{STYLE}</style></head><body><main>
<h1>🛡 Vallum status</h1>
<p class="muted">Every change to this link shortener is shipped by Vallum: agents propose, code decides, and each
release is test-driven on real traffic before everyone gets it. Revision <code>{esc(revision)}</code>.{checked}</p>
<p class="muted small">Visitors are simulated by CI jobs during the hackathon.</p>
{unreachable}{rolled}
<section><h2>Mistake allowance</h2><div class="grid">{tiers_html(modes, public.get("window_minutes"))}</div></section>
<section><h2>Releases</h2>{release_html(release, meta, data["queue"], public.get("breaker") or {})}</section>
<section><h2>Features and their promises</h2>{features_html(public, (data.get("flags") or {}).get("flags"))}</section>
<section><h2>Security</h2>{security_html(data.get("security") or {})}</section>
<section><h2>Recent releases, rollbacks and incidents</h2>{events_html(data["recent"].get("events", []), project_url)}</section>
<footer>{"See " + " and ".join(links) + ". " if links else ""}This page reads only public data and refreshes every 30 seconds.</footer>
</main></body></html>"""


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    return HTMLResponse(render(_load(request.app.state.store), request.app.state.revision),
                        headers={"Cache-Control": "public, max-age=30"})
