"""The control room's pages, one topic each: overview, releases (with the live lane), features and one page per
feature, mistake allowance, security, and the read-only rules. Pure functions from state documents to HTML."""

import json
import re
from datetime import datetime, timedelta, timezone

from control.ui import esc, markdown, mode_tone, pill, when, yaml_html

TIERS = ("core", "standard", "experimental", "app")
TIER_ORDER = {"core": 0, "standard": 1, "experimental": 2}
TYPE_ORDER = ("behavior", "speed", "reliability", "security", "data", "compatibility", "cost", "operability")
SHOWN_EVENTS = {"released", "rolled_back", "refused", "safe-mode", "live", "merged", "watchdog", "flag"}
EVENT_TONES = {"released": "good", "merged": "info", "rolled_back": "bad", "refused": "warn", "safe-mode": "bad",
               "live": "bad", "watchdog": "warn", "flag": "warn"}
MODE_WORDS = {"green": "Shipping normally.",
              "yellow": "An extra test-drive step and a size cap.",
              "red": "Changes touching it are paused; only P0 and P1 fixes go on."}
DECISIONS = (("real", "bad", "High or critical, in code we ship: becomes a fix issue that Vallum builds."),
             ("accepted", "warn", "Looked at and accepted, with the reason recorded in the Ledger."),
             ("image", "info", "In the base image's system packages, with no fixed version available yet."),
             ("not shipped", "info", "In tests or tooling that never reaches the running app."),
             ("fixed on main", "good", "Already fixed on main; it closes on the next scan."))


# ---------------------------------------------------------------- shared pieces
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
    return f"Idle. Last release: {esc(rid)} ({esc(feature)}), {esc(when(release.get('released_at')))}." if rid else "Idle."


def overall(data: dict, drive: dict) -> tuple[str, str]:
    public, meta = data.get("public") or {}, data.get("meta") or {}
    if meta.get("safe_mode"):
        return "bad", "Safe mode: nothing ships until a member resumes"
    if drive.get("phase") in ("starting", "step0", "traffic") or (data.get("release") or {}).get("slot") == "driving":
        return "brand", f"Shipping {drive.get('release_id') or (data.get('release') or {}).get('release_id') or ''} now"
    if any(p.get("live") == "broken" for f in (public.get("features") or {}).values() for p in f.get("promises") or []):
        return "bad", "A promise is broken in production"
    red = [t for t in TIERS if ((data.get("modes") or {}).get(t) or {}).get("mode") == "red"]
    if red:
        name = "The whole app" if red[0] == "app" else red[0].capitalize()
        return "warn", f"{name} is Red: risky changes are paused"
    return "good", "Every live promise is kept"


def result(p: dict, judged: dict, feature: str, checked_at) -> tuple[str, str]:
    """(tone, text) for one promise: the live check first, then the last test drive, then where it is checked."""
    if p.get("live") == "broken":
        return "bad", f"Broken in production (live check, {when(checked_at)})"
    if p.get("live") == "switched off":
        return "warn", "Not checked: the feature is switched off by its kill switch"
    if p.get("live") == "kept":
        return "good", f"Kept in production (live check, {when(checked_at)})"
    j = judged.get(f"{feature}/{p.get('id')}")
    if j:
        at = "step 0" if not j.get("step") else f"{j['step']}%"
        verdict = str(j.get("verdict") or "").replace("_", " ")
        tone = "good" if verdict == "kept" else ("info" if verdict == "collecting" else "warn")
        detail = f": {j['detail']}" if j.get("detail") else ""
        return tone, f"{verdict.capitalize()} in test drive {j.get('release')} at {at}{detail}"
    if "mr" in (p.get("where") or []):
        # a feature merges only when every one of its mr promises is kept (Gatekeeper rule 7)
        return "good", "Kept on its merge request (checked before every merge)"
    return "info", "Checked when the feature changes"


def features(public: dict, switches: dict | None = None) -> list[dict]:
    """Every live feature with its promises' results, Core first."""
    switches = switches or {}
    judged, checked_at = public.get("judged") or {}, public.get("checked_at")
    out = []
    for name, f in sorted((public.get("features") or {}).items(),
                          key=lambda kv: (TIER_ORDER.get(kv[1].get("tier"), 9), kv[0])):
        promises = f.get("promises") or []
        results = [result(p, judged, name, checked_at) for p in promises]
        kept = sum(1 for tone, _ in results if tone == "good")
        broken = sum(1 for tone, _ in results if tone == "bad")
        off = bool(f.get("flag")) and switches.get(f["flag"]) is False
        out.append({"name": name, "f": f, "promises": promises, "results": results, "kept": kept, "broken": broken,
                    "total": len(promises), "off": off, "tier": f.get("tier") or "standard",
                    "tone": "bad" if broken else ("good" if promises and kept == len(promises) else "info")})
    return out


def switch_html(item: dict, words: str = "") -> str:
    flag = item["f"].get("flag")
    if not flag:
        return '<span class="muted small">none (Core: rollback)</span>' if item["tier"] == "core" else '<span class="muted small">none</span>'
    return (f'<span class="switch{" off" if item["off"] else ""}" title="Kill switch {esc(flag)} (GitLab feature flag)">'
            f'<i aria-hidden="true"></i>{words}{"off" if item["off"] else "on"}</span>')


def meter(kept: int, total: int, tone: str) -> str:
    pct = round(100 * kept / total) if total else 0
    return f'<div class="meter {tone}"><b style="width:{pct}%"></b></div>'


def events_html(events: list[dict], project_url: str, limit: int) -> str:
    shown = [e for e in events if e.get("kind") in SHOWN_EVENTS][:limit]
    if not shown:
        return '<p class="empty">No releases yet.</p>'
    rows = []
    for e in shown:
        link, incident = "", str(e.get("incident") or "")
        if incident.isdigit() and project_url:
            link = f' <a href="{esc(project_url)}/-/issues/{incident}">incident #{incident}</a>'
        kind = str(e.get("kind"))
        rows.append(f'<tr><td class="when">{esc(when(e.get("at")))}</td>'
                    f'<td>{pill(kind.replace("_", " ").replace("-", " "), EVENT_TONES.get(kind, "info"))}</td>'
                    f'<td>{esc(e.get("summary"))}{link}</td></tr>')
    return ('<div class="tablewrap"><table class="table events"><thead><tr><th>When</th><th>Event</th><th>What happened</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def live_json(live: dict) -> str:
    return '<script type="application/json" id="live-data">' + json.dumps(live, default=str).replace("</", "<\\/") + "</script>"


def phead(title: str, lead: str, status: str = "", tone: str = "info", crumb: str = "") -> str:
    chip = f'<span class="status"><span class="dot {tone}"></span>{status}</span>' if status else ""
    return f'<div class="phead"><div>{crumb}<h1>{title}</h1><p>{lead}</p></div>{chip}</div>'


def card(title: str, body: str, sub: str = "", cls: str = "", raw: bool = False, attrs: str = "") -> str:
    inner = body if raw else f'<div class="bd">{body}</div>'
    return (f'<section class="card {cls}"{attrs}><div class="hd"><h2>{title}</h2>{f"<span>{sub}</span>" if sub else ""}</div>'
            f'{inner}</section>')


def kpi(label: str, value: str, sub: str, extra: str = "", ident: str = "", href: str = "") -> str:
    tag = "a" if href else "div"
    attrs = (f' href="{href}"' if href else "") + (f' id="{ident}"' if ident else "")
    return (f'<{tag} class="kpi"{attrs}><span class="lbl">{label}</span>'
            f'<span class="val">{value}</span><span class="sub">{sub}</span>{extra}</{tag}>')


def lowest_tier(modes: dict) -> tuple[str, int, str]:
    rows = []
    for t in TIERS:
        m = modes.get(t) or {}
        rows.append((round(100 * max(0.0, min(1.0, float(m.get("allowance", 1.0))))), t, (m.get("mode") or "green").lower()))
    left, tier, mode = min(rows)
    return ("Whole app" if tier == "app" else tier.capitalize()), left, mode


def tier_bars(modes: dict) -> str:
    out = []
    for t in TIERS:
        m = modes.get(t) or {}
        left = round(100 * max(0.0, min(1.0, float(m.get("allowance", 1.0)))))
        mode = (m.get("mode") or "green").lower()
        out.append(f'<div class="abar {mode_tone(mode)}"><span class="n">{"Whole app" if t == "app" else t.capitalize()}</span>'
                   f'<span class="t"><b style="width:{left}%"></b></span><span class="p">{left}%</span>'
                   f'{pill(mode.upper(), mode_tone(mode))}</div>')
    return "".join(out)


# ---------------------------------------------------------------- overview
FLOW = (("You", "Ask", "Move an issue to In progress, or open your own merge request."),
        ("Agents", "Build", "Planner, four guardians, Builder and Reviewer write the code and its Feature Card."),
        ("Code + Inspector", "Check", "Tests, promise checker and scans; the Inspector maps every ask to a promise."),
        ("Code", "Merge", "The Merge Desk runs 12 Gatekeeper rules and merges that exact commit."),
        ("Code", "Test drive", "Step 0 on a private address, then 10%, 50%, 100% of visitors."),
        ("Code", "Watch", "Every 10 minutes: live promises, the allowance, modes and incidents."))


def overview(data: dict, live: dict, switches: dict) -> str:
    public, drive, modes = data.get("public") or {}, live.get("drive") or {}, data.get("modes") or {}
    release, meta = data.get("release") or {}, data.get("meta") or {}
    tone, headline = overall(data, drive)
    items = features(public, switches)
    total, kept = sum(i["total"] for i in items), sum(i["kept"] for i in items)
    broken = sum(i["broken"] for i in items)
    name, left, mode = lowest_tier(modes)
    sec, counts = data.get("security") or {}, (data.get("security") or {}).get("counts") or {}
    breaker = public.get("breaker") or {}
    facts = [f"Live checks {esc(when(public.get('checked_at')))}" if public.get("checked_at") else "Live checks after the next patrol",
             "Safe mode on" if meta.get("safe_mode") else "Safe mode off",
             f"Circuit breaker {len(meta.get('rollbacks') or [])} recent rollback{'s' if len(meta.get('rollbacks') or []) != 1 else ''}"
             f" (limit {int(breaker.get('limit', 3))} in {float(breaker.get('hours', 6)):g} h)"]
    kpis = "".join([
        kpi("Now shipping", "Shipping" if release.get("slot") == "driving" else "Idle", release_text(release), ident="kpi-ship", href="/releases"),
        kpi("Promises kept", f"{kept}<small> / {total}</small>" if items else "–",
            f"{len(items)} features{f', {broken} broken' if broken else ''}" if items else "after the next Live Patrol",
            meter(kept, total, "bad" if broken else "good") if items else "", href="/features"),
        kpi("Lowest allowance", f"{left}%", f"{esc(name)} · {mode.upper()}", meter(left, 100, mode_tone(mode)), href="/allowance"),
        kpi("Open findings", str(int(sec.get("open", 0))) if sec.get("at") else "–",
            f"{int(counts.get('real', 0))} real · each decided by code" if sec.get("at") else "after the next Live Patrol", href="/security"),
    ])
    flow = "".join(f'<li><span class="n">{n}</span><b>{esc(step)}</b><p>{esc(text)}</p><span class="who">{esc(who)}</span></li>'
                   for n, (who, step, text) in enumerate(FLOW, 1))
    return f"""{phead("Overview", "Vallum ships this link shortener by itself: agents propose, code decides, and every release meets a "
                      "small share of visitors before everyone gets it.")}
<section class="banner {tone}"><span class="dot {tone}"></span><div><b>{esc(headline)}</b><p>{" · ".join(facts)}</p></div></section>
<div class="kpis">{kpis}</div>
<div class="grid21" style="margin-top:16px">
{card("Latest release", f'<div id="lane" class="lane compact"><p class="muted" style="margin:0">{release_text(release)}</p></div>', '<a class="more" href="/releases">Releases →</a>')}
{card("Mistake allowance", tier_bars(modes), '<a class="more" href="/allowance">Details →</a>')}
</div>
<div style="margin-top:16px">{card("How a change ships", f'<ol class="flow">{flow}</ol>', "Agents propose, code decides. One feature per release.")}</div>
<div style="margin-top:16px">{card("Latest activity", events_html((data.get("recent") or {}).get("events", []), public.get("project_url") or "", 6),
                                   '<a class="more" href="/releases">All releases →</a>', raw=True)}</div>
{live_json(live)}"""


# ---------------------------------------------------------------- releases
def queue_html(queue: dict, project_url: str) -> str:
    entries = queue.get("entries")
    if entries is None:          # older Merge Desk runs stored plain lines
        items = queue.get("items") or []
        if not items:
            return '<div class="bd"><p class="empty">Nothing waiting.</p></div>'
        return ('<div class="tablewrap"><table class="table"><tbody>' + "".join(f"<tr><td>{esc(i)}</td></tr>" for i in items[:8])
                + "</tbody></table></div>")
    if not entries:
        return '<div class="bd"><p class="empty">Nothing waiting.</p></div>'
    tones = {"P0": "bad", "P1": "warn", "P2": "info", "P3": "info"}
    rows = []
    for n, e in enumerate(entries[:8], 1):
        iid = int(e["iid"])
        label = f"!{iid} {esc(e.get('title'))}"
        title = f'<a href="{esc(project_url)}/-/merge_requests/{iid}">{label}</a>' if project_url else label
        state = esc(e.get("state", "")) + (f" · {esc(e['why'])}" if e.get("why") else "")
        rows.append(f'<tr><td class="num">{n}</td><td>{pill(e.get("priority") or "P2", tones.get(e.get("priority"), "info"))}</td>'
                    f'<td>{title}</td><td class="muted">{state}</td></tr>')
    return ('<div class="tablewrap"><table class="table"><thead><tr><th>#</th><th>Priority</th><th>Merge request</th><th>State</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def safety_html(meta: dict, breaker: dict) -> str:
    hours, limit = float(breaker.get("hours", 6)), int(breaker.get("limit", 3))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = 0
    for t in meta.get("rollbacks") or []:
        try:
            recent += datetime.fromisoformat(str(t).replace("Z", "+00:00")) > cutoff
        except ValueError:
            pass
    safe = meta.get("safe_mode")
    reason = f'<br><span class="small" style="color:var(--bad)">{esc(meta.get("safe_mode_reason") or "on")}</span>' if safe else ""
    return f"""<ul class="list">
<li><span class="dot {"bad" if safe else "good"}"></span><span><b>Safe mode {"ON" if safe else "off"}</b>{reason}<br>
<span class="muted small">After too many rollbacks, no merges and no releases until a member resumes.</span></span></li>
<li><span class="dot {"warn" if recent else "good"}"></span><span><b>Circuit breaker {recent} of {limit}</b> rollbacks in {hours:g} h<br>
<span class="muted small">At {limit} rollbacks within {hours:g} hours, Safe mode turns on by itself.</span></span></li>
<li><span class="dot brand"></span><span><b>Rollback in seconds</b><br>
<span class="muted small">A broken promise during a test drive moves every visitor back to the old version.</span></span></li></ul>"""


def releases(data: dict, live: dict) -> str:
    public, release = data.get("public") or {}, data.get("release") or {}
    project_url = public.get("project_url") or ""
    active = release.get("slot") in ("driving", "merged", "gated")
    return f"""{phead("Releases", "Each release meets a private check first, then a growing share of visitors. Any broken "
                     "promise rolls it back in seconds.", release_text(release), "brand" if active else "info")}
{card("Test drive", f'<div id="lane" class="lane"><p class="muted" style="margin:0">{release_text(release)}</p></div>', "live, updates by itself")}
<div class="grid21" style="margin-top:16px">
{card("Release queue", queue_html(data.get("queue") or {}, project_url), "priority first, then age; one release at a time", raw=True)}
{card("Safety nets", safety_html(data.get("meta") or {}, public.get("breaker") or {}), "what stops a bad release, by code")}
</div>
<div style="margin-top:16px">{card("History", events_html((data.get("recent") or {}).get("events", []), project_url, 20),
                                   "releases, rollbacks, refusals and incidents, newest first", raw=True)}</div>
{live_json(live)}"""


# ---------------------------------------------------------------- features
def _type_rank(t) -> int:
    return TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER)


def features_page(public: dict, switches: dict) -> str:
    items = features(public, switches)
    lead = ("Every feature is a Feature Card in the repo: what it promises, checked before every merge, during the "
            "test drive and in production. Open a feature to see its promises.")
    if not items:
        return phead("Features", lead) + '<p class="empty">The Live Patrol publishes the live features after its next run.</p>'
    counts = {}
    for i in items:
        counts[i["tier"]] = counts.get(i["tier"], 0) + 1
    filters = f'<button type="button" class="on" data-tier="all">All <b>{len(items)}</b></button>' + "".join(
        f'<button type="button" data-tier="{t}">{t.capitalize()} <b>{counts[t]}</b></button>'
        for t in ("core", "standard", "experimental") if counts.get(t))
    total, kept = sum(i["total"] for i in items), sum(i["kept"] for i in items)
    rows = []
    for i in items:
        f = i["f"]
        types = {}
        for p in i["promises"]:
            types[p.get("type")] = types.get(p.get("type"), 0) + 1
        chips = "".join(f"<span>{esc(t)} {n}</span>" for t, n in sorted(types.items(), key=lambda kv: _type_rank(kv[0])))
        issue = f.get("issue")
        origin = f"from issue #{int(issue)}" if issue and str(issue).isdigit() else "base feature"
        broken = f' <span style="color:var(--bad)">· {i["broken"]} broken</span>' if i["broken"] else ""
        rows.append(
            f'<tr class="link fcard {esc(i["tier"])}{" broken" if i["broken"] else ""}" data-tier="{esc(i["tier"])}" data-href="/features/{esc(i["name"])}">'
            f'<td><a class="fname" href="/features/{esc(i["name"])}">{esc(i["name"])}</a>'
            f'<div class="muted small">{esc(f.get("kind") or "")} · {origin}</div><div class="types">{chips}</div></td>'
            f'<td>{pill(i["tier"].capitalize(), i["tier"])} {pill(f.get("priority") or "P2")}</td>'
            f'<td>{switch_html(i)}</td>'
            f'<td class="kept"><span>{i["kept"]} of {i["total"]} promises kept{broken}</span>{meter(i["kept"], i["total"], i["tone"])}</td>'
            f'<td class="go" aria-hidden="true">›</td></tr>')
    table = ('<div class="tablewrap"><table class="table ftable"><thead><tr><th>Feature</th><th>Tier</th><th>Kill switch</th>'
             f'<th>Promises</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    return (phead("Features", lead, f"{kept} of {total} promises kept", "good" if kept == total else "warn")
            + f'<div class="filters" role="group" aria-label="Filter by tier">{filters}</div>'
            + card("Live features", table, "click a row for its promises and card file", raw=True))


def feature_page(public: dict, switches: dict, name: str) -> str | None:
    item = next((i for i in features(public, switches) if i["name"] == name), None)
    if item is None:
        return None
    f, project_url = item["f"], public.get("project_url") or ""
    issue = f.get("issue")
    origin = (f'<a href="{esc(project_url)}/-/work_items/{int(issue)}">issue #{int(issue)}</a>'
              if issue and str(issue).isdigit() and project_url else "base feature")
    routes = "".join(f'<span class="chip">{esc(r)}</span>' for r in f.get("routes") or []) or '<span class="muted">none</span>'
    groups = {}
    for p, res in zip(item["promises"], item["results"]):
        groups.setdefault(p.get("type") or "other", []).append((p, res))
    where_words = {"mr": "merge request", "drive": "test drive", "live": "production"}
    rows = []
    for t in sorted(groups, key=_type_rank):
        rows.append(f'<tr class="group"><td colspan="4">{esc(t)} · {len(groups[t])}</td></tr>')
        for p, (tone, text) in groups[t]:
            extra = (" " + pill("watch", "brand") if p.get("source") == "watch" else "") + (" " + pill("learned", "lime") if p.get("source") == "learned" else "")
            where = "".join(f'<span class="chip">{esc(where_words.get(w, w))}</span>' for w in p.get("where") or [])
            rows.append(f'<tr><td><span class="dot {tone}"></span></td><td><b class="pid">{esc(p.get("id"))}</b>{extra}'
                        f'<div class="says">{esc(p.get("says"))}</div><div class="res {tone}">{esc(text)}</div></td>'
                        f'<td>{pill(p.get("severity") or "", "bad" if p.get("severity") == "blocker" else "info")}</td>'
                        f'<td><div class="where">{where}</div></td></tr>')
    promises = ('<div class="tablewrap"><table class="table ptable"><thead><tr><th></th><th>Promise and latest result</th><th>Severity</th>'
                f'<th>Checked in</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>') if rows else '<div class="bd"><p class="empty">No promises.</p></div>'
    card_file = f.get("path") or ""
    open_link = f'<a class="more" href="{esc(project_url)}/-/blob/main/{esc(card_file)}" rel="noopener">Open on GitLab ↗</a>' if project_url and card_file else ""
    crumb = f'<div class="crumb"><a href="/features">Features</a> / {esc(name)}</div>'
    kpis = "".join([
        kpi("Promises kept", f'{item["kept"]}<small> / {item["total"]}</small>', f'{item["broken"]} broken' if item["broken"] else "none broken",
            meter(item["kept"], item["total"], item["tone"])),
        kpi("Tier", pill(item["tier"].capitalize(), item["tier"]) + " " + pill(f.get("priority") or "P2"), esc(f.get("kind") or "")),
        kpi("Kill switch", f'<span class="vt">{switch_html(item, "kill switch ")}</span>',
            "a GitLab feature flag" if f.get("flag") else "Core features roll back instead"),
        kpi("Origin", f'<span class="vt">{origin}</span>', routes),
    ])
    yaml_block = yaml_html(f.get("yaml") or "") if f.get("yaml") else '<p class="empty">Published with the next Live Patrol.</p>'
    return f"""{phead(esc(name), f'{esc(f.get("kind") or "")} · {item["kept"]} of {item["total"]} promises kept', crumb=crumb)}
<div class="kpis">{kpis}</div>
<div class="dgrid" style="margin-top:16px">{card("Promises", promises, "grouped by type", raw=True)}
<section class="card cardfile"><div class="hd"><h2>Card file</h2>{open_link}</div><div class="bd"><p class="mono muted" style="margin:0 0 10px">{esc(card_file)}</p>{yaml_block}</div></section></div>"""


# ---------------------------------------------------------------- allowance
def allowance(data: dict) -> str:
    modes, public = data.get("modes") or {}, data.get("public") or {}
    window = public.get("window_minutes")
    span = f" in the last {window:g} min" if window else ""
    cards = []
    for t in TIERS:
        m = modes.get(t) or {}
        left = round(100 * max(0.0, min(1.0, float(m.get("allowance", 1.0)))))
        mode = (m.get("mode") or "green").lower()
        visits, bad, allowed = int(m.get("visits", 0)), int(m.get("bad", 0)), m.get("allowed_bad")
        detail = (f"{bad} bad of {visits:,} visits; {round(float(allowed))} allowed{span}" if visits and allowed is not None
                  else f"No visits{span}")
        label = "Whole app" if t == "app" else t.capitalize()
        cards.append(f'<div class="card tcard"><div class="tt"><h3>{esc(label)}</h3>{pill(mode.upper(), mode_tone(mode))}</div>'
                     f'<div class="gauge {mode_tone(mode)}" style="--p:{left}" role="img" aria-label="{esc(label)}: {left}% of the allowance left">'
                     f'<span><b>{left}%</b><small>left</small></span></div>'
                     f'<p class="small">{esc(detail)}</p><p class="mw">{esc(MODE_WORDS.get(mode, ""))}</p></div>')
    lead = ("Each tier may afford a few bad visits per hour. A visit is bad if it fails or is twice as slow as its "
            "promise. Spend the allowance and the tier's mode changes, by code.")
    modes_table = """<div class="tablewrap"><table class="table"><thead><tr><th>Mode</th><th>What happens</th></tr></thead><tbody>
<tr><td><span class="pill good">GREEN</span></td><td>Ship normally.</td></tr>
<tr><td><span class="pill warn">YELLOW</span></td><td>Standard and the whole app only: an extra 25% test-drive step and a size cap on changes.</td></tr>
<tr><td><span class="pill bad">RED</span></td><td>New work touching the tier pauses with the reason; P0 and P1 fixes still go through.</td></tr></tbody></table></div>"""
    thresholds = """<div class="tablewrap"><table class="table"><thead><tr><th>Tier</th><th>Enter</th><th>Leave</th></tr></thead><tbody>
<tr><td><span class="pill core">Core</span></td><td>Red below 50%</td><td>Green above 65% (no Yellow: the gap stops flickering)</td></tr>
<tr><td><span class="pill standard">Standard</span></td><td>Yellow below 50%, Red below 10%</td><td>Yellow above 25%, Green above 60%</td></tr>
<tr><td><span class="pill experimental">Experimental</span></td><td>Always Green</td><td>It can still roll back</td></tr>
<tr><td><span class="pill">Whole app</span></td><td colspan="2">Like Standard, against the service's target</td></tr></tbody></table></div>"""
    name, left, mode = lowest_tier(modes)
    return f"""{phead("Mistake allowance", lead, f"Lowest: {esc(name)} {left}%", mode_tone(mode))}<div class="tiers4">{"".join(cards)}</div>
<div class="grid2" style="margin-top:16px">{card("What the modes do", modes_table, "set by code, never by an agent", raw=True)}
{card("When modes change", thresholds, "separate thresholds to enter and to leave", raw=True)}</div>"""


# ---------------------------------------------------------------- security
def security(sec: dict, project_url: str) -> str:
    lead = ("SAST, secret detection and container scanning run on every change. Every open finding gets a decision "
            "made by code; a new high or critical finding blocks the merge (Gatekeeper rule 8).")
    if not sec.get("at"):
        return phead("Security", lead) + '<p class="empty">The Live Patrol triages scanner findings on its next run.</p>'
    counts, total = sec.get("counts") or {}, int(sec.get("open", 0))
    real = int(counts.get("real", 0))
    rows = "".join(f'<tr><td class="num big">{int(counts.get(k, 0))}</td><td>{pill(k, tone)}</td><td class="muted">{esc(text)}</td></tr>'
                   for k, tone, text in DECISIONS)
    report = (f'<a class="more" href="{esc(project_url)}/-/security/vulnerability_report" rel="noopener">Vulnerability report ↗</a>'
              if project_url else "")
    kpis = "".join([
        kpi("Open findings", f'<span class="secbig">{total}</span>', "on main, all three scanners"),
        kpi("Real", str(real), "fix issues opened for Vallum to build" if real else "none in the code we ship"),
        kpi("In the base image", str(int(counts.get("image", 0))), "system packages, no fix available yet"),
        kpi("Last checked", esc(when(sec["at"])).replace(" UTC", ""), "UTC, by the Live Patrol"),
    ])
    scanners = ('<ul class="list"><li><span class="dot brand"></span><span><b>SAST</b><br><span class="muted small">Reads the code for '
                'security bugs without running it.</span></span></li><li><span class="dot brand"></span><span><b>Secret detection</b><br>'
                '<span class="muted small">Looks for passwords or keys committed by mistake.</span></span></li><li><span class="dot brand">'
                '</span><span><b>Container scanning</b><br><span class="muted small">Checks the image\'s packages for known vulnerabilities; '
                'step 0 judges it before any visitor.</span></span></li></ul>')
    return f"""{phead("Security", lead, "No real finding" if not real else f"{real} real", "bad" if real else "good")}
<div class="kpis">{kpis}</div>
<div class="grid21" style="margin-top:16px">{card("Decisions", '<div class="tablewrap"><table class="table dec"><thead><tr><th>Count</th><th>Decision</th><th>Meaning</th></tr></thead><tbody>' + rows + '</tbody></table></div>', report, raw=True)}
{card("Scanners", scanners, "run on every change")}</div>"""


# ---------------------------------------------------------------- rules (read-only copies)
def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-") or "doc"


def rules(docs: dict, project_url: str, chosen: str | None) -> str | None:
    files = docs.get("files") or []
    lead = ("What every agent reads before it works, and what code enforces: the Rulebook, the policy, the catalog of "
            "allowed changes, the coding guidelines, the code map and the project profile.")
    if not files:
        return phead("Rules and code", lead) + '<p class="empty">The Live Patrol publishes a copy of the rules and the code map on its next run.</p>'
    current = next((f for f in files if slug(f.get("title")) == chosen), None) if chosen else files[0]
    if current is None:
        return None
    toc = "".join(f'<a href="/rules/{slug(f.get("title"))}" class="{"on" if f is current else ""}">{esc(f.get("title"))}'
                  f'<small>{esc(f.get("path"))}</small></a>' for f in files)
    path = current.get("path") or ""
    base = f"{project_url}/-/blob/main/{path.rsplit('/', 1)[0]}" if project_url and "/" in path else (
        f"{project_url}/-/blob/main" if project_url else "")
    text = current.get("text") or ""
    rendered = markdown(text, base) if current.get("format") == "md" else yaml_html(text)
    open_link = f'<a class="more" href="{esc(project_url)}/-/blob/main/{esc(path)}" rel="noopener">Open on GitLab ↗</a>' if project_url else ""
    commit = (docs.get("commit") or "")[:8]
    at = f" at {esc(commit)}" if commit else ""
    refreshed = f" ({esc(when(docs.get('at')))})" if docs.get("at") else ""
    note = (f'<p class="note info">🔒 Read-only copy of <b>main</b>{at}, refreshed by every Live Patrol{refreshed}. These '
            'files change only through a merge request; agents can never change the Rulebook, the policy or the code map.</p>')
    return f"""{phead("Rules and code", lead)}{note}<div class="rules"><nav class="card toc" aria-label="Documents">{toc}</nav>
<section class="card doc"><div class="hd"><h2>{esc(current.get("title"))}</h2><span class="mono">{esc(path)}</span>{open_link}</div><div class="bd">{rendered}</div></section></div>"""


# ---------------------------------------------------------------- styles and the live script
CSS = """
.tablewrap{overflow-x:auto}.table td.when{white-space:nowrap;color:var(--muted);font-size:12.5px;width:140px}.table td.num{width:40px;color:var(--muted);font-weight:600}
.banner{display:flex;gap:12px;align-items:flex-start;background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--good);border-radius:var(--r);padding:14px 18px;margin:0 0 16px;box-shadow:var(--shadow)}
.banner .dot{margin-top:6px}.banner b{font-size:15px}.banner p{margin:2px 0 0;color:var(--muted);font-size:13px}
.banner.warn{border-left-color:#d97706}.banner.bad{border-left-color:var(--bad)}.banner.brand{border-left-color:var(--brand)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}@media (max-width:1060px){.kpis{grid-template-columns:1fr 1fr}}@media (max-width:520px){.kpis{grid-template-columns:1fr}}
.kpi{display:flex;flex-direction:column;gap:4px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;box-shadow:var(--shadow);color:var(--ink);min-width:0}
a.kpi:hover{border-color:#c7cdf9;text-decoration:none}
.kpi .lbl{font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.kpi .val{font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1.2;display:flex;gap:6px;align-items:center;flex-wrap:wrap}.kpi .val small{font-size:15px;color:var(--muted);font-weight:600}
.kpi .vt{font-size:16px;font-weight:600;letter-spacing:0}.kpi .sub{font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}.kpi .meter{margin-top:6px}
.meter{height:6px;border-radius:999px;background:#e8ecf2;overflow:hidden}.meter b{display:block;height:100%;background:var(--good);border-radius:999px}
.meter.bad b{background:var(--bad)}.meter.warn b{background:#d97706}.meter.info b{background:#94a3b8}
.abar{display:grid;grid-template-columns:96px 1fr 40px 64px;gap:10px;align-items:center;padding:7px 0;font-size:13px}.abar .n{font-weight:600}
.abar .t{height:8px;border-radius:999px;background:#e8ecf2;overflow:hidden}.abar .t b{display:block;height:100%;border-radius:999px;background:var(--good)}
.abar.warn .t b{background:#d97706}.abar.bad .t b{background:var(--bad)}.abar .p{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}.abar .pill{justify-self:start}
.flow{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(6,1fr);gap:0;position:relative}
@media (max-width:1060px){.flow{grid-template-columns:repeat(3,1fr);row-gap:18px}}@media (max-width:560px){.flow{grid-template-columns:1fr}}
.flow li{position:relative;padding:0 12px 0 0}
.flow li::after{content:"";position:absolute;top:14px;left:34px;right:8px;height:2px;background:var(--line)}.flow li:last-child::after{display:none}
.flow .n{position:relative;z-index:1;width:28px;height:28px;border-radius:50%;background:var(--brand-soft);color:var(--brand-ink);border:1px solid #d6daff;display:grid;place-items:center;font-size:12.5px;font-weight:700}
.flow b{display:block;margin:10px 0 2px;font-size:14px}.flow p{margin:0 0 8px;font-size:12.5px;color:var(--muted);line-height:1.5}
.flow .who{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:5px;background:var(--sunken);border:1px solid var(--line);color:var(--ink2)}
.lane-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}.lane-head h3{font-size:16px;font-weight:600}
.track{display:flex;align-items:flex-start;margin:4px 0 18px}.stop{display:flex;flex-direction:column;align-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--muted);min-width:56px}
.stop .c{width:30px;height:30px;border-radius:50%;border:2px solid var(--line2);background:var(--surface);display:grid;place-items:center;font-size:13px}
.stop.done .c{background:var(--good);border-color:var(--good);color:#fff}.stop.fail .c{background:var(--bad);border-color:var(--bad);color:#fff}
.stop.now .c{background:var(--brand);border-color:var(--brand);color:#fff;animation:pulse 1.4s infinite}.stop.now{color:var(--brand-ink)}
.seg{flex:1;height:2px;background:var(--line2);margin-top:14px}.seg.done{background:var(--good)}
.split{display:flex;height:30px;border-radius:8px;overflow:hidden;background:#e8ecf2;font-size:12.5px;font-weight:600}
.split .new{background:var(--brand);color:#fff;display:flex;align-items:center;padding:0 12px;white-space:nowrap;transition:width .8s ease}
.split .old{flex:1;display:flex;align-items:center;justify-content:flex-end;padding:0 12px;color:var(--muted);white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}@media (max-width:800px){.stats{grid-template-columns:1fr 1fr}}
.stat{background:var(--sunken);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:0}.stat span{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.stat b{display:block;font-size:17px;font-weight:700;overflow-wrap:anywhere}
.timer{height:4px;background:var(--line);border-radius:4px;margin-top:6px;overflow:hidden}.timer i{display:block;height:100%;background:var(--brand);transition:width 1s linear}
details.log{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}details.log summary{cursor:pointer;font-weight:600;font-size:13.5px}
details.log pre{font-family:var(--mono);font-size:11.5px;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:12px 14px;max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:10px 0 0}
.lane.compact .stats,.lane.compact details.log{display:none}.lane.compact .lane-head{margin-bottom:12px}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(67,56,202,.45)}70%{box-shadow:0 0 0 8px rgba(67,56,202,0)}100%{box-shadow:0 0 0 0 rgba(67,56,202,0)}}
.list{list-style:none;margin:0;padding:0}.list>li{display:flex;gap:12px;align-items:flex-start;padding:11px 0;border-top:1px solid var(--line)}
.list>li:first-child{border-top:0;padding-top:0}.list>li:last-child{padding-bottom:0}.list .dot{margin-top:6px}
.filters{display:inline-flex;gap:2px;padding:3px;margin:0 0 14px;background:var(--surface);border:1px solid var(--line);border-radius:10px}
.filters button{font:inherit;font-size:13px;font-weight:600;border:0;background:transparent;color:var(--ink2);padding:6px 12px;border-radius:7px;cursor:pointer}
.filters button b{font-weight:600;color:var(--muted);margin-left:2px}.filters button.on{background:var(--brand);color:#fff}.filters button.on b{color:#c7cdf9}
.ftable .fname{font-weight:600;font-size:14.5px;color:var(--ink)}.ftable td{vertical-align:middle}
.ftable tr.broken td:first-child{box-shadow:inset 3px 0 0 var(--bad)}
.types{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}.types span{font-size:11px;font-weight:500;color:var(--muted);background:var(--sunken);border:1px solid var(--line);padding:0 6px;border-radius:5px}
.ftable td.kept{min-width:190px}.ftable td.kept span{display:block;font-size:12.5px;font-weight:600;margin-bottom:6px}.ftable td.go{color:var(--muted);font-size:20px;width:24px}
.switch{display:inline-flex;gap:7px;align-items:center;font-size:12.5px;font-weight:600;color:var(--ink2)}
.switch i{width:28px;height:16px;border-radius:999px;background:var(--good);position:relative;flex:none}.switch i::after{content:"";position:absolute;top:2px;left:14px;width:12px;height:12px;border-radius:50%;background:#fff}
.switch.off i{background:#cbd5e1}.switch.off i::after{left:2px}
.dgrid{display:grid;grid-template-columns:1.35fr .65fr;gap:16px;align-items:start}.dgrid>*,.rules>*{min-width:0}@media (max-width:1060px){.dgrid{grid-template-columns:1fr}}
.ptable tr.group td{background:var(--sunken);font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);padding:7px 14px}
.ptable td:first-child{width:22px;padding-right:0}.ptable .dot{display:inline-block;margin-top:5px}.ptable .says{margin:2px 0}.ptable .res{font-size:12.5px;font-weight:500}
.res.good{color:var(--good)}.res.warn{color:var(--warn)}.res.bad{color:var(--bad)}.res.info{color:var(--muted)}
.where{display:flex;gap:4px;flex-wrap:wrap}.where .chip{font-family:inherit;font-size:11px}
.cardfile{position:sticky;top:20px}.cardfile .yaml{max-height:64vh}@media (max-width:1060px){.cardfile{position:static}}
.tiers4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}@media (max-width:1060px){.tiers4{grid-template-columns:1fr 1fr}}@media (max-width:520px){.tiers4{grid-template-columns:1fr}}
.tcard{padding:16px 18px}.tcard .tt{display:flex;justify-content:space-between;align-items:center}.tcard h3{font-size:15px;font-weight:600}
.gauge{--c:var(--good);width:112px;height:112px;margin:14px auto;border-radius:50%;background:conic-gradient(var(--c) calc(var(--p)*1%),#e8ecf2 0);display:grid;place-items:center}
.gauge.warn{--c:#d97706}.gauge.bad{--c:var(--bad)}.gauge span{width:88px;height:88px;border-radius:50%;background:var(--surface);display:grid;place-items:center;align-content:center;line-height:1.1}
.gauge b{font-size:22px;font-weight:700}.gauge small{font-size:11.5px;color:var(--muted)}
.tcard p{margin:0;color:var(--muted);text-align:center}.tcard p.mw{margin-top:6px;color:var(--ink2);font-weight:500;font-size:13px}
.secbig{font-size:26px}.dec td.big{font-size:18px;font-weight:700;color:var(--ink)}
.rules{display:grid;grid-template-columns:250px 1fr;gap:16px;align-items:start}@media (max-width:880px){.rules{grid-template-columns:1fr}}
.toc{padding:6px;position:sticky;top:20px}.toc a{display:block;padding:9px 12px;border-radius:8px;color:var(--ink2);font-weight:600;text-decoration:none}
.toc a:hover{background:var(--sunken)}.toc a.on{background:var(--brand-soft);color:var(--brand-ink)}.toc small{display:block;font-family:var(--mono);font-size:10.5px;color:var(--muted);font-weight:400}
@media (max-width:880px){.toc{position:static;display:flex;overflow-x:auto;gap:4px}.toc a{white-space:nowrap}.toc small{display:none}}
.doc .hd .mono{margin-right:auto}
@media (max-width:700px){.table thead{display:none}.table,.table tbody,.table tr,.table td{display:block;width:100%}
.table tr{padding:10px 16px;border-bottom:1px solid var(--line)}.table tr:last-child{border-bottom:0}.table td{padding:3px 0;border:0}
.table td.go,.ptable td:first-child{display:none}.table td.when,.table td.num{width:auto}.ftable td.kept{min-width:0;padding-top:8px}
.ptable tr.group{padding:0}.ptable tr.group td{padding:7px 16px}.abar{grid-template-columns:84px 1fr 38px}.abar .pill{display:none}}
"""

FILTER_SCRIPT = r"""
const buttons = [...document.querySelectorAll('.filters button')], rows = [...document.querySelectorAll('tr.fcard')];
buttons.forEach(b => b.onclick = () => {
  buttons.forEach(x => x.classList.toggle('on', x === b));
  rows.forEach(r => r.hidden = b.dataset.tier !== 'all' && r.dataset.tier !== b.dataset.tier);
});
rows.forEach(r => r.onclick = e => { if (!e.target.closest('a')) location.href = r.dataset.href; });
"""

LIVE_SCRIPT = r"""
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ago = iso => { if (!iso) return ''; const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  return s < 90 ? Math.round(s) + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' days ago'; };
const ACTIVE = ['starting', 'step0', 'traffic'];
let logOpen = false;
function state(data) {
  const d = data.drive || {};
  const active = ACTIVE.includes(d.phase) && Date.now() - Date.parse(d.updated_at || 0) < 15 * 60e3;
  const steps = [0].concat(d.steps || []), verdicts = {};
  (d.verdicts || []).forEach(v => verdicts[v.step] = v.verdict);
  const pct = d.phase === 'released' ? 100 : (d.phase === 'traffic' && active ? Number(d.pct || 0) : 0);
  const stops = steps.map(s => {
    let cls = '';
    if (verdicts[s] === 'kept' || d.phase === 'released') cls = 'done';
    else if (verdicts[s] && verdicts[s] !== 'kept') cls = 'fail';
    else if (active && ((s === 0 && d.phase !== 'traffic') || (d.phase === 'traffic' && s === d.pct))) cls = 'now';
    else if ((s === 0 && d.phase === 'refused') || (d.phase === 'rolled_back' && s === d.pct)) cls = 'fail';
    return {label: s === 0 ? 'Step 0' : s + '%', cls};
  });
  const head = active ? 'Shipping now' : d.phase === 'released' ? 'Released' : d.phase === 'refused' ? 'Refused at step 0' :
    d.phase === 'rolled_back' ? 'Rolled back' : 'Stopped';
  const tone = active ? 'brand' : d.phase === 'released' ? 'good' : 'bad';
  const where = !active && d.phase !== 'released' && !['refused', 'rolled_back'].includes(d.phase) ? 'This test drive stopped without a result; the Watchdog cleans it up.' :
    d.phase === 'step0' || d.phase === 'starting' ? 'Step 0: the new version runs on a private address; no visitor sees it yet.' :
    d.phase === 'traffic' ? pct + '% of visitors are on the new version; the rest stay on the current one.' :
    d.phase === 'released' ? 'Every visitor is on the new version.' : (d.reason || '');
  return {d, active, pct, stops, head, tone, where};
}
function renderLane(st, data) {
  const el = $('lane'); if (!el) return;
  const d = st.d;
  if (!d.release_id) { el.innerHTML = '<p class="empty">No release yet. When a change merges, its test drive appears here live.</p>'; return; }
  const mr = d.mr_iid && data.project_url ? ' · <a href="' + esc(data.project_url) + '/-/merge_requests/' + Number(d.mr_iid) + '">!' + Number(d.mr_iid) + '</a>' : '';
  const watch = Number(d.watch_s || 0), elapsed = Number(d.elapsed || 0), tw = watch ? Math.min(100, 100 * elapsed / watch) : 0;
  const stats = st.active ? '<div class="stats">' +
    '<div class="stat"><span>This step</span><b>' + elapsed + ' / ' + watch + ' s</b><div class="timer"><i style="width:' + tw + '%"></i></div></div>' +
    '<div class="stat"><span>Visits (new version)</span><b>' + Number(d.visits || 0) + ' (' + Number(d.visits_new || 0) + ')</b></div>' +
    '<div class="stat"><span>Judgment</span><b>' + esc(d.status || (d.phase === 'step0' ? 'checking' : 'starting')) + '</b></div>' +
    '<div class="stat"><span>Closest promise</span><b style="font-size:13px">' + esc(d.worst || '—') + '</b></div></div>' : '';
  const when = st.active ? 'updated ' + ago(d.updated_at) : 'finished ' + ago(d.finished_at || d.updated_at);
  const track = st.stops.map((s, i) => (i ? '<span class="seg ' + (s.cls === 'done' ? 'done' : '') + '"></span>' : '') +
    '<span class="stop ' + s.cls + '"><span class="c">' + (s.cls === 'done' ? '✓' : s.cls === 'fail' ? '✕' : '') + '</span>' + s.label + '</span>').join('');
  el.innerHTML = '<div class="lane-head"><span class="pill ' + st.tone + '">' + st.head + '</span><h3>' + esc(d.title || d.feature || '') + '</h3><span class="muted small">' +
    esc(d.release_id || '') + ' · ' + esc((d.tier || '').replace(/^./, c => c.toUpperCase())) + mr + ' · ' + when + '</span></div>' +
    '<div class="track" aria-label="Test-drive steps">' + track + '</div>' +
    '<div class="split" role="img" aria-label="' + st.pct + '% of visitors on the new version"><div class="new" style="width:' + Math.max(st.pct, st.pct ? 8 : 0) + '%;min-width:' + (st.pct ? 76 : 0) + 'px">' + (st.pct ? 'new ' + st.pct + '%' : '') + '</div>' + (st.pct < 100 ? '<div class="old">current ' + (100 - st.pct) + '%</div>' : '') + '</div>' +
    '<p class="muted small" style="margin:10px 0 0">' + esc(st.where) + '</p>' + stats +
    '<details class="log" id="lane-log"' + (logOpen ? ' open' : '') + '><summary>Live log</summary><pre>' + esc((d.log || []).join('\n') || 'No log lines yet.') + '</pre></details>';
  $('lane-log').ontoggle = e => { logOpen = e.target.open; };
}
function renderKpi(st) {
  const el = $('kpi-ship'); if (!el || !st.d.release_id) return;
  el.querySelector('.val').textContent = st.active ? (st.d.phase === 'traffic' ? st.pct + '%' : 'Step 0') : st.head;
  el.querySelector('.sub').textContent = st.d.release_id + ' · ' + (st.d.feature || '') + ' · ' + (st.active ? 'live' : ago(st.d.finished_at || st.d.updated_at));
}
function render(data) { const st = state(data); renderLane(st, data); renderKpi(st); return st; }
async function poll() {
  let wait = 20000;
  try {
    const r = await fetch('/live.json', {cache: 'no-store'});
    if (r.ok) { const st = render(await r.json()); if (st.active) wait = 3000; }
  } catch (e) {}
  setTimeout(poll, wait);
}
const first = render(JSON.parse($('live-data').textContent));
setTimeout(poll, first.active ? 3000 : 15000);
"""
