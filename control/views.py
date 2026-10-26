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
    if drive.get("phase") in ("starting", "step0", "traffic"):
        return "teal", f"Shipping {drive.get('release_id') or ''} now"
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
        return ""
    return (f'<span class="switch{" off" if item["off"] else ""}" title="Kill switch {esc(flag)} (GitLab feature flag)">'
            f'<i aria-hidden="true"></i>{words}{"off" if item["off"] else "on"}</span>')


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
        rows.append(f'<li><span class="when">{esc(when(e.get("at")))}</span>'
                    f'{pill(kind.replace("_", " ").replace("-", " "), EVENT_TONES.get(kind, "info"))}'
                    f'<span>{esc(e.get("summary"))}{link}</span></li>')
    return '<ul class="list events">' + "".join(rows) + "</ul>"


def live_json(live: dict) -> str:
    return '<script type="application/json" id="live-data">' + json.dumps(live, default=str).replace("</", "<\\/") + "</script>"


def phead(title: str, lead: str, eyebrow: str = "", tone: str = "info", crumb: str = "") -> str:
    eb = f'<span class="eyebrow"><span class="dot {tone}"></span>{eyebrow}</span>' if eyebrow else ""
    return f'<div class="phead">{crumb}{eb}<h1>{title}</h1><p>{lead}</p></div>'


# ---------------------------------------------------------------- overview
FLOW = (("You", "Ask", "Move an issue to In progress, or open your own merge request."),
        ("Agents", "Build", "Planner, four guardians, Builder and Reviewer write the code and its Feature Card."),
        ("Code + Inspector", "Check", "Tests, promise checker and scans; the Inspector maps every ask to a promise."),
        ("Code", "Merge", "The Merge Desk runs 12 Gatekeeper rules and merges that exact commit."),
        ("Code", "Test drive", "Step 0 on a private address, then 10%, 50%, 100% of visitors."),
        ("Code", "Watch", "Every 10 minutes: live promises, the allowance, modes and incidents."))


def overview(data: dict, live: dict, switches: dict) -> str:
    public, drive = data.get("public") or {}, live.get("drive") or {}
    tone, headline = overall(data, drive)
    items = features(public, switches)
    total = sum(i["total"] for i in items)
    kept = sum(i["kept"] for i in items)
    broken = sum(i["broken"] for i in items)
    dots = "".join(f'<span class="{i["tone"]}" title="{esc(i["name"])}: {i["kept"]} of {i["total"]} kept"></span>' for i in items)
    promises_viz = (f'<div class="vbox center"><div class="big">{kept}<small> / {total}</small></div>'
                    f'<div class="muted small">promises kept{f" · {broken} broken" if broken else ""}</div>'
                    f'<div class="fdots">{dots}</div></div>') if items else '<div class="vbox center muted small">Published after the next Live Patrol.</div>'
    promises_text = (f"{len(items)} features, each a Feature Card in the repo, checked before merge, in the test drive "
                     "and in production.") if items else "Every feature is a Feature Card in the repo."

    modes = data.get("modes") or {}
    bars, worst = [], None
    for t in TIERS:
        m = modes.get(t) or {}
        left = round(100 * max(0.0, min(1.0, float(m.get("allowance", 1.0)))))
        mode = (m.get("mode") or "green").lower()
        if mode == "red" and worst is None:
            worst = t
        bars.append(f'<div class="abar {mode_tone(mode)}"><span>{"Whole app" if t == "app" else t.capitalize()}</span>'
                    f'<span class="t"><b style="width:{left}%"></b></span><span>{left}%</span></div>')
    allowance_text = (f"{'The whole app' if worst == 'app' else worst.capitalize()} is Red: changes touching it are paused "
                      "until the allowance recovers." if worst else "Every tier is within its allowance: shipping normally.")

    sec = data.get("security") or {}
    counts = sec.get("counts") or {}
    if sec.get("at"):
        chips = " ".join(pill(f"{counts[k]} {k}", t) for k, t, _ in DECISIONS if counts.get(k))
        security_viz = (f'<div class="vbox center"><div class="big">{int(sec.get("open", 0))}</div>'
                        f'<div class="muted small">open findings, each decided by code</div><div class="chips">{chips}</div></div>')
        real = int(counts.get("real", 0))
        security_text = (f"{real} real high or critical finding{'s' if real != 1 else ''}: fix issues opened." if real
                         else "No real high or critical finding in the code we ship.")
    else:
        security_viz = '<div class="vbox center muted small">Triaged on the next Live Patrol.</div>'
        security_text = "Every scanner finding gets a decision made by code."

    flow = "".join(f'<li><span class="n">{n}</span><b>{esc(step)}</b><p>{esc(text)}</p><span class="who">{esc(who)}</span></li>'
                   for n, (who, step, text) in enumerate(FLOW, 1))
    return f"""<section class="hero"><span class="eyebrow"><span class="dot {tone}"></span>{esc(headline)}</span>
<h1>Every change is test-driven before it reaches you.</h1>
<p>Vallum ships this link shortener by itself: agents propose, code decides, and every release meets a small share of
visitors before everyone gets it.</p></section>
<section class="tiles">
<a class="tile" href="/releases"><h2>Now shipping.</h2><div class="viz" id="mini"><div class="vbox center muted small">{release_text(data.get("release") or {})}</div></div>
<p id="mini-text">{release_text(data.get("release") or {})}</p><span class="go">Releases →</span></a>
<a class="tile" href="/features"><h2>Promises kept.</h2><div class="viz">{promises_viz}</div><p>{esc(promises_text)}</p><span class="go">Features →</span></a>
<a class="tile" href="/allowance"><h2>Mistake allowance.</h2><div class="viz"><div class="vbox">{"".join(bars)}</div></div><p>{esc(allowance_text)}</p><span class="go">Allowance →</span></a>
<a class="tile" href="/security"><h2>Security.</h2><div class="viz">{security_viz}</div><p>{esc(security_text)}</p><span class="go">Security →</span></a>
</section>
<section class="panel" style="margin-top:22px"><h2>How a change ships</h2><p class="sub">Agents propose, code decides. One feature per release.</p><ol class="flow">{flow}</ol></section>
<section class="panel" style="margin-top:20px"><h2>Latest activity</h2><p class="sub">Releases, rollbacks and incidents, newest first.</p>
{events_html((data.get("recent") or {}).get("events", []), public.get("project_url") or "", 5)}
<p style="margin:14px 0 0"><a class="more" href="/releases">All releases →</a></p></section>
{live_json(live)}"""


# ---------------------------------------------------------------- releases
def queue_html(queue: dict, project_url: str) -> str:
    entries = queue.get("entries")
    if entries is None:          # older Merge Desk runs stored plain lines
        items = queue.get("items") or []
        return ('<ul class="list">' + "".join(f"<li>{esc(i)}</li>" for i in items[:8]) + "</ul>") if items else \
            '<p class="empty">Nothing waiting.</p>'
    if not entries:
        return '<p class="empty">Nothing waiting.</p>'
    tones = {"P0": "bad", "P1": "warn", "P2": "info", "P3": "info"}
    rows = []
    for n, e in enumerate(entries[:8], 1):
        iid = int(e["iid"])
        label = f"!{iid} {esc(e.get('title'))}"
        title = f'<a href="{esc(project_url)}/-/merge_requests/{iid}">{label}</a>' if project_url else label
        state = esc(e.get("state", "")) + (f" · {esc(e['why'])}" if e.get("why") else "")
        rows.append(f'<li><span class="qn">{n}</span>{pill(e.get("priority") or "P2", tones.get(e.get("priority"), "info"))}'
                    f'<span>{title}<br><span class="muted small">{state}</span></span></li>')
    return '<ul class="list queue">' + "".join(rows) + "</ul>"


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
    return f"""<ul class="list safety">
<li><span class="dot {"bad" if safe else "good"}"></span><span><b>Safe mode {"ON" if safe else "off"}</b>{reason}<br>
<span class="muted small">After too many rollbacks, no merges and no releases until a member resumes.</span></span></li>
<li><span class="dot {"warn" if recent else "good"}"></span><span><b>Circuit breaker {recent} of {limit}</b> rollbacks in {hours:g} h<br>
<span class="muted small">At {limit} rollbacks within {hours:g} hours, Safe mode turns on by itself.</span></span></li>
<li><span class="dot teal"></span><span><b>Rollback in seconds</b><br>
<span class="muted small">A broken promise during a test drive moves every visitor back to the old version.</span></span></li></ul>"""


def releases(data: dict, live: dict) -> str:
    public, release = data.get("public") or {}, data.get("release") or {}
    project_url = public.get("project_url") or ""
    active = release.get("slot") in ("driving", "merged", "gated")
    return f"""{phead("Releases", "Each release meets a private check first, then a growing share of visitors. Any broken "
                     "promise rolls it back in seconds.", release_text(release), "teal" if active else "info")}
<section id="lane" class="panel lane"><p class="muted" style="margin:0">{release_text(release)}</p></section>
<div class="grid2" style="margin-top:20px">
<section class="panel"><h2>Release queue</h2><p class="sub">Priority first, then age. One release at a time.</p>{queue_html(data.get("queue") or {}, project_url)}</section>
<section class="panel"><h2>Safety nets</h2><p class="sub">What stops a bad release, by code.</p>{safety_html(data.get("meta") or {}, public.get("breaker") or {})}</section>
</div>
<section class="panel" style="margin-top:20px"><h2>History</h2><p class="sub">Releases, rollbacks, refusals and incidents, newest first.</p>
{events_html((data.get("recent") or {}).get("events", []), project_url, 20)}</section>
{live_json(live)}"""


# ---------------------------------------------------------------- features
def features_page(public: dict, switches: dict) -> str:
    items = features(public, switches)
    lead = ("Every feature is a Feature Card in the repo: what it promises, checked before every merge, during the "
            "test drive and in production. Open a card to see its promises.")
    if not items:
        return phead("Features and their promises", lead) + '<p class="empty">The Live Patrol publishes the live features after its next run.</p>'
    counts = {}
    for i in items:
        counts[i["tier"]] = counts.get(i["tier"], 0) + 1
    filters = f'<button type="button" class="on" data-tier="all">All {len(items)}</button>' + "".join(
        f'<button type="button" data-tier="{t}">{t.capitalize()} {counts[t]}</button>' for t in ("core", "standard", "experimental") if counts.get(t))
    cards = []
    for i in items:
        f, pct = i["f"], round(100 * i["kept"] / i["total"]) if i["total"] else 0
        types = {}
        for p in i["promises"]:
            types[p.get("type")] = types.get(p.get("type"), 0) + 1
        chips = "".join(f"<span>{esc(t)} {n}</span>" for t, n in sorted(types.items(), key=lambda kv: _type_rank(kv[0])))
        issue = f.get("issue")
        origin = f"from issue #{int(issue)}" if issue and str(issue).isdigit() else "base feature"
        broken = f'<span style="color:var(--bad)">{i["broken"]} broken</span>' if i["broken"] else ""
        cards.append(
            f'<a class="fcard {esc(i["tier"])}{" broken" if i["broken"] else ""}" data-tier="{esc(i["tier"])}" href="/features/{esc(i["name"])}">'
            f'<div class="ftop">{pill(i["tier"].capitalize(), i["tier"])}{pill(f.get("priority") or "P2")}{switch_html(i)}</div>'
            f'<h3>{esc(i["name"])}</h3><div class="kind">{esc(f.get("kind") or "")} · {origin}</div>'
            f'<div class="kept"><span>{i["kept"]} of {i["total"]} promises kept</span>{broken}</div>'
            f'<div class="meter {i["tone"]}"><b style="width:{pct}%"></b></div>'
            f'<div class="types">{chips}</div><span class="open">View promises →</span></a>')
    return (phead("Features and their promises", lead) + f'<div class="filters" role="group" aria-label="Filter by tier">{filters}</div>'
            f'<div class="fgrid">{"".join(cards)}</div>')


def _type_rank(t) -> int:
    return TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER)


def feature_page(public: dict, switches: dict, name: str) -> str | None:
    item = next((i for i in features(public, switches) if i["name"] == name), None)
    if item is None:
        return None
    f, project_url = item["f"], public.get("project_url") or ""
    issue = f.get("issue")
    origin = (f'<a href="{esc(project_url)}/-/work_items/{int(issue)}">from issue #{int(issue)}</a>'
              if issue and str(issue).isdigit() and project_url else "a base feature")
    routes = "".join(f'<span class="chip">{esc(r)}</span>' for r in f.get("routes") or [])
    groups = {}
    for p, res in zip(item["promises"], item["results"]):
        groups.setdefault(p.get("type") or "other", []).append((p, res))
    where_words = {"mr": "merge request", "drive": "test drive", "live": "production"}
    blocks = []
    for t in sorted(groups, key=_type_rank):
        rows = []
        for p, (tone, text) in groups[t]:
            extra = (pill("watch", "teal") if p.get("source") == "watch" else "") + (pill("learned", "lime") if p.get("source") == "learned" else "")
            where = "".join(f'<span class="chip">{esc(where_words.get(w, w))}</span>' for w in p.get("where") or [])
            rows.append(f'<li><span class="dot {tone}"></span><div><div><span class="pid">{esc(p.get("id"))}</span> '
                        f'{pill(p.get("severity") or "", "bad" if p.get("severity") == "blocker" else "info")} {extra}</div>'
                        f'<div class="says">{esc(p.get("says"))}</div><div class="res {tone}">{esc(text)}</div>'
                        f'<div class="where">{where}</div></div></li>')
        blocks.append(f'<div class="ptype"><h3>{esc(t)} · {len(rows)}</h3><ul class="list prom">{"".join(rows)}</ul></div>')
    card_file = f.get("path") or ""
    open_link = f'<a class="more" href="{esc(project_url)}/-/blob/main/{esc(card_file)}" rel="noopener">Open on GitLab ↗</a>' if project_url and card_file else ""
    crumb = f'<div class="crumb"><a href="/features">Features</a> / {esc(name)}</div>'
    lead = f'{esc(f.get("kind") or "")} · {origin} · {item["kept"]} of {item["total"]} promises kept'
    return f"""<div class="phead">{crumb}<h1>{esc(name)}</h1><p>{lead}</p>
<div class="fhead">{pill(item["tier"].capitalize(), item["tier"])}{pill(f.get("priority") or "P2")}{switch_html(item, "kill switch ")}{routes}</div></div>
<div class="dgrid"><section class="panel"><h2>Promises</h2><p class="sub">Grouped by type. Each says where it is checked.</p>{"".join(blocks) or '<p class="empty">No promises.</p>'}</section>
<aside class="panel cardfile"><h2>Card file</h2><p class="sub"><span class="mono">{esc(card_file)}</span></p>
{yaml_html(f.get("yaml") or "") if f.get("yaml") else '<p class="empty">Published with the next Live Patrol.</p>'}
<p style="margin:14px 0 0">{open_link}</p></aside></div>"""


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
        cards.append(f'<div class="tcard"><div class="ring {mode_tone(mode)}" style="--p:{left}" role="img" '
                     f'aria-label="{esc(label)}: {left}% of the allowance left"><span><b>{left}%</b><small>left</small></span></div>'
                     f'<h3>{esc(label)} {pill(mode.upper(), mode_tone(mode))}</h3><p>{esc(detail)}</p>'
                     f'<p><b style="color:var(--ink2)">{esc(MODE_WORDS.get(mode, ""))}</b></p></div>')
    lead = ("Each tier may afford a few bad visits per hour. A visit is bad if it fails or is twice as slow as its "
            "promise. Spend the allowance and the tier's mode changes, by code.")
    modes_list = """<ul class="list modes">
<li><span class="dot good"></span><span><b>Green</b><br><span class="muted small">Ship normally.</span></span></li>
<li><span class="dot warn"></span><span><b>Yellow</b> (Standard and the whole app)<br><span class="muted small">An extra 25% test-drive step and a size cap on changes.</span></span></li>
<li><span class="dot bad"></span><span><b>Red</b><br><span class="muted small">New work touching the tier pauses with the reason; P0 and P1 fixes still go through.</span></span></li></ul>"""
    thresholds = """<ul class="list modes">
<li><span class="pill core">Core</span><span class="small">Red below 50%; back to Green above 65%. No Yellow: the gap stops flickering.</span></li>
<li><span class="pill standard">Standard</span><span class="small">Yellow below 50%, Red below 10%; back to Yellow above 25% and Green above 60%.</span></li>
<li><span class="pill experimental">Experimental</span><span class="small">Always Green; it can still roll back.</span></li>
<li><span class="pill">Whole app</span><span class="small">Like Standard, against the service's target.</span></li></ul>"""
    return f"""{phead("Mistake allowance", lead)}<div class="tiers4">{"".join(cards)}</div>
<div class="grid2"><section class="panel"><h2>What the modes do</h2><p class="sub">Set by code from the allowance, never by an agent.</p>{modes_list}</section>
<section class="panel"><h2>When modes change</h2><p class="sub">Separate thresholds to enter and to leave.</p>{thresholds}</section></div>"""


# ---------------------------------------------------------------- security
def security(sec: dict, project_url: str) -> str:
    lead = ("SAST, secret detection and container scanning run on every change. Every open finding gets a decision "
            "made by code; a new high or critical finding blocks the merge (Gatekeeper rule 8).")
    if not sec.get("at"):
        return phead("Security", lead) + '<p class="empty">The Live Patrol triages scanner findings on its next run.</p>'
    counts, total = sec.get("counts") or {}, int(sec.get("open", 0))
    rows = "".join(f'<li><span class="n">{int(counts.get(k, 0))}</span><span>{pill(k, tone)}<br>'
                   f'<span class="muted small">{esc(text)}</span></span></li>' for k, tone, text in DECISIONS)
    report = (f'<p style="margin:16px 0 0"><a class="more" href="{esc(project_url)}/-/security/vulnerability_report" '
              'rel="noopener">Vulnerability report on GitLab ↗</a></p>') if project_url else ""
    real = int(counts.get("real", 0))
    return f"""{phead("Security", lead, f"Checked {esc(when(sec['at']))}", "bad" if real else "good")}
<div class="grid2"><section class="panel"><h2>Open findings</h2><p class="sub">On main, from all three scanners.</p>
<div class="secbig">{total}</div><p class="muted" style="margin:6px 0 0">{"No real high or critical finding in the code we ship." if not real else f"{real} real: fix issues opened for Vallum to build."}</p>
<div class="chips" style="margin-top:16px;justify-content:flex-start"><span class="chip">SAST</span><span class="chip">Secret detection</span><span class="chip">Container scanning</span></div>{report}</section>
<section class="panel"><h2>Decisions</h2><p class="sub">Made by code on every Live Patrol.</p><ul class="list dec">{rows}</ul></section></div>"""


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
    return f"""{phead("Rules and code", lead)}{note}<div class="rules"><nav class="panel toc" aria-label="Documents">{toc}</nav>
<article class="panel doc"><h2>{esc(current.get("title"))}</h2><div class="meta"><span class="mono muted">{esc(path)}</span>{open_link}</div>{rendered}</article></div>"""


# ---------------------------------------------------------------- styles and the live script
CSS = """
.hero{text-align:center;padding:28px 0 38px}.hero h1{font-size:clamp(36px,5.8vw,66px);font-weight:800;letter-spacing:-.045em;line-height:1.03;margin:20px auto 16px;max-width:920px}
.hero p{font-size:18px;color:var(--muted);max-width:660px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:18px}@media (max-width:1080px){.tiles{grid-template-columns:repeat(2,1fr)}}@media (max-width:580px){.tiles{grid-template-columns:1fr}}
.tile{background:var(--soft);border:1px solid transparent;border-radius:28px;padding:26px 24px 22px;display:flex;flex-direction:column;min-height:380px;color:var(--ink);text-decoration:none;transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}
.tile:hover{transform:translateY(-3px);box-shadow:var(--shadow);border-color:var(--line)}@media (max-width:580px){.tile{min-height:0}.hero{padding-top:8px}}.tile h2{font-size:25px;font-weight:800}
.tile .viz{flex:1;display:flex;align-items:center;padding:20px 0}.tile p{margin:0;color:var(--ink2);font-size:14.5px;line-height:1.55}
.tile .go{margin-top:14px;font-weight:700;font-size:13.5px;color:var(--teal)}
.vbox{background:var(--surface);border-radius:20px;padding:16px 18px;width:100%;box-shadow:0 1px 2px rgba(40,32,16,.05),0 8px 20px rgba(40,32,16,.04)}
.vbox.center{text-align:center}.big{font-size:46px;font-weight:800;letter-spacing:-.045em;line-height:1}.big small{font-size:18px;color:var(--muted);font-weight:700;letter-spacing:0}
.fdots{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:14px}.fdots span{width:15px;height:15px;border-radius:5px;background:var(--good)}
.fdots span.bad{background:var(--bad)}.fdots span.info{background:#c9c3b5}
.chips{display:flex;gap:6px;flex-wrap:wrap;justify-content:center;margin-top:12px}
.abar{display:grid;grid-template-columns:84px 1fr 38px;gap:10px;align-items:center;font-size:12.5px;font-weight:700;padding:5px 0}
.abar .t{height:9px;border-radius:999px;background:var(--soft2);overflow:hidden}.abar .t b{display:block;height:100%;border-radius:999px;background:var(--good)}
.abar.warn .t b{background:#e0a92a}.abar.bad .t b{background:var(--bad)}.abar span:last-child{text-align:right}
.mtrack{display:flex;align-items:center}.mtrack .st{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:800;background:var(--soft2);color:var(--muted);border:2px solid var(--line);flex:none}
.mtrack .st.done{background:var(--good);border-color:var(--good);color:#fff}.mtrack .st.fail{background:var(--bad);border-color:var(--bad);color:#fff}
.mtrack .st.now{background:var(--lime);border-color:#b7d23a;color:var(--ink);animation:pulse 1.4s infinite}
.mtrack i{flex:1;height:3px;background:var(--line);min-width:6px}.mtrack i.done{background:var(--good)}
.msplit{height:10px;border-radius:999px;background:var(--soft2);overflow:hidden;margin-top:16px}.msplit b{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#5ec2d4,#b9e36a);transition:width .8s ease}
.mlab{display:flex;justify-content:space-between;font-size:12px;font-weight:700;color:var(--muted);margin-top:8px}
.flow{list-style:none;margin:18px 0 0;padding:0;display:grid;grid-template-columns:repeat(6,1fr);gap:12px}
@media (max-width:1080px){.flow{grid-template-columns:repeat(3,1fr)}}@media (max-width:580px){.flow{grid-template-columns:1fr}}
.flow li{background:var(--soft2);border-radius:20px;padding:16px;display:flex;flex-direction:column}
.flow .n{width:30px;height:30px;border-radius:50%;background:var(--surface);border:1px solid var(--line);display:grid;place-items:center;font-size:13px;font-weight:800}
.flow b{font-size:16px;margin:10px 0 4px}.flow p{margin:0 0 10px;font-size:13.5px;color:var(--muted);line-height:1.5;flex:1}
.flow .who{align-self:flex-start;font-size:11.5px;font-weight:800;padding:2px 9px;border-radius:999px;background:var(--surface);color:var(--ink2)}
.events li{align-items:center;flex-wrap:wrap}.events .when{min-width:118px;color:var(--muted);font-size:13px;font-weight:600}
.lane.active{border-color:#b7d23a;box-shadow:0 0 0 4px var(--lime-soft),var(--shadow)}
.lane-head{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:24px}.lane-head h2{font-size:24px;font-weight:800}
.track{display:flex;align-items:flex-start;margin:4px 0 24px}.stop{display:flex;flex-direction:column;align-items:center;gap:8px;font-size:13px;font-weight:700;color:var(--muted);min-width:64px}
.stop .c{width:42px;height:42px;border-radius:50%;border:2px solid var(--line);background:var(--surface);display:grid;place-items:center;font-size:16px}
.stop.done .c{background:var(--good);border-color:var(--good);color:#fff}.stop.fail .c{background:var(--bad);border-color:var(--bad);color:#fff}
.stop.now .c{background:var(--lime);border-color:#b7d23a;color:var(--ink);animation:pulse 1.4s infinite}.stop.now{color:var(--ink)}
.seg{flex:1;height:4px;background:var(--line);border-radius:4px;margin-top:19px}.seg.done{background:var(--good)}
.split{display:flex;height:44px;border-radius:16px;overflow:hidden;background:var(--soft2);font-size:13.5px;font-weight:700}
.split .new{background:linear-gradient(90deg,#5ec2d4,#b9e36a);color:var(--ink);display:flex;align-items:center;padding:0 14px;white-space:nowrap;transition:width .8s ease}
.split .old{flex:1;display:flex;align-items:center;justify-content:flex-end;padding:0 14px;color:var(--muted);white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}@media (max-width:800px){.stats{grid-template-columns:1fr 1fr}}
.stat{background:var(--soft2);border-radius:16px;padding:12px 14px;min-width:0}.stat b{display:block;font-size:19px;font-weight:800;overflow-wrap:anywhere}
.timer{height:6px;background:var(--line);border-radius:6px;margin-top:8px;overflow:hidden}.timer i{display:block;height:100%;background:var(--teal);transition:width 1s linear}
details.log{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}details.log summary{cursor:pointer;font-weight:700}
details.log pre{font-family:var(--mono);font-size:12px;background:var(--soft2);border-radius:14px;padding:14px;max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:10px 0 0}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(183,210,58,.7)}70%{box-shadow:0 0 0 10px rgba(183,210,58,0)}100%{box-shadow:0 0 0 0 rgba(183,210,58,0)}}
.queue li,.safety li,.modes li,.dec li{align-items:center}.qn{width:26px;height:26px;border-radius:50%;background:var(--soft2);display:grid;place-items:center;font-size:12.5px;font-weight:800;flex:none}
.safety .dot,.modes .dot{margin-top:2px}.modes .pill{min-width:104px;justify-content:center}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 20px}
.filters button{font:inherit;font-size:14px;font-weight:700;border:1px solid var(--line);background:var(--surface);color:var(--ink2);padding:7px 15px;border-radius:999px;cursor:pointer}
.filters button.on{background:var(--dark);color:#fff;border-color:var(--dark)}
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:18px}
.fcard{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--line);border-radius:24px;padding:22px 22px 20px;color:var(--ink);text-decoration:none;box-shadow:var(--shadow);transition:transform .15s ease;position:relative;overflow:hidden}
.fcard::before{content:"";position:absolute;inset:0 0 auto 0;height:5px;background:var(--standard)}.fcard.core::before{background:var(--core)}.fcard.experimental::before{background:var(--experimental)}
.fcard:hover{transform:translateY(-3px)}.fcard.broken{border-color:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)}
.ftop{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:14px}.fcard h3{font-size:21px;font-weight:800}
.fcard .kind{font-size:13.5px;color:var(--muted);margin:3px 0 16px}
.kept{display:flex;justify-content:space-between;font-size:13.5px;font-weight:700;margin-bottom:7px}
.meter{height:8px;border-radius:999px;background:var(--soft2);overflow:hidden}.meter b{display:block;height:100%;background:var(--good);border-radius:999px}
.meter.bad b{background:var(--bad)}.meter.info b{background:var(--info)}
.types{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 0}.types span{font-size:12px;font-weight:600;color:var(--muted);background:var(--soft2);padding:2px 8px;border-radius:8px}
.fcard .open{margin-top:auto;padding-top:16px;font-weight:700;font-size:13.5px;color:var(--teal)}
.switch{display:inline-flex;gap:6px;align-items:center;font-size:12.5px;font-weight:700;color:var(--muted);margin-left:auto}
.switch i{width:28px;height:16px;border-radius:999px;background:var(--good);position:relative;flex:none}.switch i::after{content:"";position:absolute;top:2px;left:14px;width:12px;height:12px;border-radius:50%;background:#fff}
.switch.off i{background:#cfc8b9}.switch.off i::after{left:2px}
.fhead{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:16px 0 0}.fhead .switch{margin-left:0}
.dgrid{display:grid;grid-template-columns:1.25fr .75fr;gap:20px;align-items:start}.dgrid>*,.rules>*{min-width:0}@media (max-width:980px){.dgrid{grid-template-columns:1fr}}
.ptype{margin:0 0 18px}.ptype h3{font-size:12.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:0 0 4px}
.prom .dot{margin-top:9px}.prom .pid{font-weight:800}.prom .says{margin:2px 0}.prom .res{font-size:13.5px;font-weight:600}
.res.good{color:var(--good)}.res.warn{color:var(--warn)}.res.bad{color:var(--bad)}.res.info{color:var(--muted)}
.prom .where{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}.prom .where .chip{font-family:inherit;font-size:11.5px;font-weight:600}
.cardfile{position:sticky;top:100px}.cardfile .yaml{max-height:62vh}@media (max-width:980px){.cardfile{position:static}}
.tiers4{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-bottom:20px}@media (max-width:1080px){.tiers4{grid-template-columns:1fr 1fr}}@media (max-width:580px){.tiers4{grid-template-columns:1fr}}
.tcard{background:var(--surface);border:1px solid var(--line);border-radius:24px;padding:24px 20px;box-shadow:var(--shadow);text-align:center}
.ring{--c:var(--good);width:124px;height:124px;margin:2px auto 16px;border-radius:50%;background:conic-gradient(var(--c) calc(var(--p)*1%),var(--soft2) 0);display:grid;place-items:center}
.ring.warn{--c:#e0a92a}.ring.bad{--c:var(--bad)}.ring span{width:98px;height:98px;border-radius:50%;background:var(--surface);display:grid;place-items:center;align-content:center;line-height:1.05}
.ring b{font-size:27px;font-weight:800}.ring small{font-size:12px;color:var(--muted);font-weight:600}
.tcard h3{font-size:19px;font-weight:800;display:flex;gap:8px;justify-content:center;align-items:center}.tcard p{margin:8px 0 0;font-size:13.5px;color:var(--muted)}
.secbig{font-size:68px;font-weight:800;letter-spacing:-.045em;line-height:1}.dec .n{font-size:24px;font-weight:800;min-width:58px}
.rules{display:grid;grid-template-columns:260px 1fr;gap:20px;align-items:start}@media (max-width:880px){.rules{grid-template-columns:1fr}}
.toc{padding:10px;position:sticky;top:100px}.toc a{display:block;padding:10px 14px;border-radius:14px;color:var(--ink2);text-decoration:none;font-weight:700}
.toc a:hover{background:var(--soft2)}.toc a.on{background:var(--soft);color:var(--ink)}.toc small{display:block;font-family:var(--mono);font-size:11px;color:var(--muted);font-weight:400}
@media (max-width:880px){.toc{position:static;display:flex;overflow-x:auto;gap:4px}.toc a{white-space:nowrap}.toc small{display:none}}
.doc{min-width:0}.doc .meta{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:4px 0 18px}
"""

FILTER_SCRIPT = r"""
const buttons = [...document.querySelectorAll('.filters button')], cards = [...document.querySelectorAll('.fcard')];
buttons.forEach(b => b.onclick = () => {
  buttons.forEach(x => x.classList.toggle('on', x === b));
  cards.forEach(c => c.hidden = b.dataset.tier !== 'all' && c.dataset.tier !== b.dataset.tier);
});
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
    return {label: s === 0 ? 'Step 0' : s + '%', short: s === 0 ? '0' : s + '%', cls};
  });
  const head = active ? 'Shipping now' : d.phase === 'released' ? 'Released' : d.phase === 'refused' ? 'Refused at step 0' :
    d.phase === 'rolled_back' ? 'Rolled back' : 'Stopped';
  const tone = active ? 'lime' : d.phase === 'released' ? 'good' : 'bad';
  const where = !active && d.phase !== 'released' && !['refused', 'rolled_back'].includes(d.phase) ? 'This test drive stopped without a result; the Watchdog cleans it up.' :
    d.phase === 'step0' || d.phase === 'starting' ? 'Step 0: the new version runs on a private address; no visitor sees it yet.' :
    d.phase === 'traffic' ? pct + '% of visitors are on the new version; the rest stay on the current one.' :
    d.phase === 'released' ? 'Every visitor is on the new version.' : (d.reason || '');
  return {d, active, pct, stops, head, tone, where};
}
function track(st, cls, short) {
  return st.stops.map((s, i) => (i ? '<' + (short ? 'i' : 'span') + ' class="' + (short ? '' : 'seg ') + (s.cls === 'done' ? 'done' : '') + '"></' + (short ? 'i' : 'span') + '>' : '') +
    (short ? '<span class="st ' + s.cls + '">' + (s.cls === 'fail' ? '✕' : s.short) + '</span>'
           : '<span class="stop ' + s.cls + '"><span class="c">' + (s.cls === 'done' ? '✓' : s.cls === 'fail' ? '✕' : '') + '</span>' + s.label + '</span>')).join('');
}
function renderLane(st, data) {
  const el = $('lane'); if (!el) return;
  const d = st.d;
  el.className = 'panel lane' + (st.active ? ' active' : '');
  if (!d.release_id) { el.innerHTML = '<div class="lane-head"><span class="pill info">No release yet</span><h2>All quiet</h2></div><p class="muted" style="margin:0">When a change merges, its test drive appears here live: step 0 on a private address, then a growing share of visitors.</p>'; return; }
  const mr = d.mr_iid && data.project_url ? ' · <a href="' + esc(data.project_url) + '/-/merge_requests/' + Number(d.mr_iid) + '">!' + Number(d.mr_iid) + '</a>' : '';
  const watch = Number(d.watch_s || 0), elapsed = Number(d.elapsed || 0), tw = watch ? Math.min(100, 100 * elapsed / watch) : 0;
  const stats = st.active ? '<div class="stats">' +
    '<div class="stat"><span class="muted small">This step</span><b>' + elapsed + ' / ' + watch + ' s</b><div class="timer"><i style="width:' + tw + '%"></i></div></div>' +
    '<div class="stat"><span class="muted small">Visits (on the new version)</span><b>' + Number(d.visits || 0) + ' (' + Number(d.visits_new || 0) + ')</b></div>' +
    '<div class="stat"><span class="muted small">Judgment</span><b>' + esc(d.status || (d.phase === 'step0' ? 'checking' : 'starting')) + '</b></div>' +
    '<div class="stat"><span class="muted small">Closest promise</span><b style="font-size:13.5px">' + esc(d.worst || '—') + '</b></div></div>' : '';
  const when = st.active ? 'updated ' + ago(d.updated_at) : 'finished ' + ago(d.finished_at || d.updated_at);
  el.innerHTML = '<div class="lane-head"><span class="pill ' + st.tone + '">' + st.head + '</span><h2>' + esc(d.title || d.feature || '') + '</h2><span class="muted small">' +
    esc(d.release_id || '') + ' · ' + esc((d.tier || '').replace(/^./, c => c.toUpperCase())) + mr + ' · ' + when + '</span></div>' +
    '<div class="track" aria-label="Test-drive steps">' + track(st) + '</div>' +
    '<div class="split" role="img" aria-label="' + st.pct + '% of visitors on the new version"><div class="new" style="width:' + Math.max(st.pct, st.pct ? 8 : 0) + '%;min-width:' + (st.pct ? 76 : 0) + 'px">' + (st.pct ? 'new ' + st.pct + '%' : '') + '</div>' + (st.pct < 100 ? '<div class="old">current ' + (100 - st.pct) + '%</div>' : '') + '</div>' +
    '<p class="muted small" style="margin:12px 0 0">' + esc(st.where) + '</p>' + stats +
    '<details class="log" id="lane-log"' + (logOpen ? ' open' : '') + '><summary>Live log</summary><pre>' + esc((d.log || []).join('\n') || 'No log lines yet.') + '</pre></details>';
  $('lane-log').ontoggle = e => { logOpen = e.target.open; };
}
function renderMini(st) {
  const el = $('mini'); if (!el || !st.d.release_id) return;
  const d = st.d;
  el.innerHTML = '<div class="vbox"><div class="mtrack">' + track(st, '', true) + '</div><div class="msplit"><b style="width:' + st.pct + '%"></b></div>' +
    '<div class="mlab"><span>' + esc(d.release_id) + ' · ' + esc(d.feature || '') + '</span><span>' + (st.active ? st.pct + '% live' : esc(st.head.toLowerCase())) + '</span></div></div>';
  $('mini-text').textContent = st.active ? 'Test drive ' + d.release_id + ' (' + (d.feature || '') + '): ' + st.where :
    st.head + ' ' + ago(d.finished_at || d.updated_at) + ': ' + (d.title || d.feature || '') + '.';
}
function render(data) { const st = state(data); renderLane(st, data); renderMini(st); return st; }
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
