"""Mistake allowance and modes (B10). Plain code: every visit is good or bad, allowances are computed over a
sliding window per tier and app-wide, and modes change with separate enter and leave thresholds (hysteresis).

    allowance = 1 - bad / max(min_allowed_bad, (1 - target) x visits)      clamped to [0, 1]
    (the floor keeps a short demo window from flipping on one slow visit: simulated visitors run from CI
    runners, and their network adds a rare slow request that no user of the app would see)
    Core:         Green -> Red below 50%, Red -> Green above 65%      (no Yellow)
    Standard:     Green -> Yellow below 50%, Yellow -> Red below 10%, Red -> Yellow above 25%, Yellow -> Green above 60%
    Experimental: always Green
    App-wide:     like Standard, against the service target
"""

from datetime import timedelta

from vallum.checker.measure import parse_measure
from vallum.config import iso, parse_iso, utcnow

TIERS = ("core", "standard", "experimental")
DOC = "allowance"


def route_profile(cards: dict, service: dict | None = None) -> tuple[dict, dict]:
    """route -> tier, and route -> slow threshold (ms) from the strictest speed promise on that route."""
    tier_of, limit = {}, {}
    rank = {"experimental": 1, "standard": 2, "core": 3}
    for card in cards.values():
        tier = card.get("tier", "standard")
        for r in (card.get("owns") or {}).get("routes") or []:
            if rank.get(tier, 0) >= rank.get(tier_of.get(r), 0):
                tier_of[r] = tier
        for ptype in ("speed",):
            for p in (card.get("promises") or {}).get(ptype) or []:
                if "measure" not in p:
                    continue
                try:
                    m = parse_measure(p["measure"])
                except ValueError:
                    continue
                if m.route != "*" and not m.filters and m.func.startswith("p"):
                    limit[m.route] = min(limit.get(m.route, m.limit), m.limit)
    return tier_of, limit


def count(samples: list[dict], tier_of: dict, limit: dict, slow_factor: float = 2.0, warmup_s: float = 0,
          revision: str | None = None, blame: str | None = None) -> dict:
    """Good and bad visits per tier and app-wide. Bad = failed, or slower than slow_factor x the route's limit.
    `blame`: during a test drive, only bad visits served by that (new) revision count against the allowance;
    the old revision's slow visits under the simulator's load are not the release's fault."""
    out = {t: {"visits": 0, "bad": 0} for t in (*TIERS, "app")}
    for s in samples:
        if s.get("t", 0) < warmup_s or (revision is not None and s.get("revision") != revision):
            continue
        tier = tier_of.get(s.get("route"))
        lim = limit.get(s.get("route"))
        bad = (not s.get("ok", False)) or (lim is not None and (s.get("ms") or 0) > lim * slow_factor)
        if blame is not None and s.get("revision") != blame:
            bad = False
        for key in ([tier] if tier in TIERS else []) + ["app"]:
            out[key]["visits"] += 1
            out[key]["bad"] += int(bad)
    return out


def record(store, counts: dict, source: str, spend: dict | None = None, window_minutes: float = 60):
    """Add one bucket of counts (and direct spends, e.g. a broken live behavior promise) and drop old ones."""
    entry = {"at": iso(), "source": source, "counts": counts, "spend": spend or {}}
    cutoff = iso(utcnow() - timedelta(minutes=window_minutes))

    def fn(cur):
        buckets = [b for b in cur.get("buckets", []) if b.get("at", "") >= cutoff]
        buckets.append(entry)
        return {"buckets": buckets[-500:]}

    store.update(DOC, fn)
    return entry


def compute(buckets: list[dict], targets: dict, window_minutes: float, now=None, min_allowed_bad: float = 0) -> dict:
    now = now or utcnow()
    cutoff = now - timedelta(minutes=window_minutes)
    total = {t: {"visits": 0, "bad": 0, "spend": 0.0} for t in (*TIERS, "app")}
    for b in buckets:
        at = parse_iso(b.get("at"))
        if at and at < cutoff:
            continue
        for t, c in (b.get("counts") or {}).items():
            if t in total:
                total[t]["visits"] += int(c.get("visits", 0))
                total[t]["bad"] += int(c.get("bad", 0))
        for t, amount in (b.get("spend") or {}).items():
            if t in total:
                total[t]["spend"] += float(amount)
    result = {}
    for t, v in total.items():
        allowed = (1 - targets.get(t, 0.995)) * v["visits"]
        if v["visits"]:
            allowed = max(allowed, float(min_allowed_bad))
        used = (v["bad"] / allowed) if allowed > 0 else (1.0 if v["bad"] else 0.0)
        remaining = max(0.0, min(1.0, 1.0 - used - v["spend"]))
        result[t] = {"allowance": round(remaining, 4), "visits": v["visits"], "bad": v["bad"],
                     "allowed_bad": round(allowed, 2)}
    return result


def next_mode(tier: str, current: str, allowance: float, tiers_policy: dict, standard_yellow_exit: float = 0.60) -> str:
    if tier == "experimental":
        return "green"
    cfg = tiers_policy.get("core" if tier == "core" else "standard", {})
    red_below = cfg.get("red_below") or 0
    recover = cfg.get("recover_above") or 1
    if tier == "core":
        if current == "red":
            return "green" if allowance > recover else "red"
        return "red" if allowance < red_below else "green"
    yellow_below = cfg.get("yellow_below") or 0
    if current == "red":
        if allowance > recover:
            return "green" if allowance > standard_yellow_exit else "yellow"
        return "red"
    if allowance < red_below:
        return "red"
    if current == "yellow":
        return "green" if allowance > standard_yellow_exit else "yellow"
    return "yellow" if allowance < yellow_below else "green"


WORDS = {"red": "RED: changes touching it are paused (only P0/P1 fixes continue)",
         "yellow": "YELLOW: an extra test-drive step and a size cap",
         "green": "GREEN: shipping normally"}


def announce(changes: list[dict], store, notify, owner: str | None):
    """Tell the owner about every mode change (event + alert); used by the Live Patrol and the test drive."""
    for c in changes:
        store.event("mode", f"{c['tier']} {c['from'].upper()} → {c['to'].upper()} ({round(100 * c['allowance'])}% left)")
        notify.alert("issues", None, f"{c['tier'].capitalize()} is now {c['to'].upper()}", author=owner,
                     what=f"The {c['tier']} mistake allowance is at {round(100 * c['allowance'])}%, so its mode "
                          f"changed from {c['from'].upper()} to {c['to'].upper()}.",
                     impact=WORDS[c["to"]], done="Updated the modes; the Merge Desk and the Flight Director follow them.",
                     nxt="Modes change back by themselves as the allowance recovers.", action="No.")


def update_modes(store, pol) -> list[dict]:
    """Recompute allowances and modes in one transaction; return the mode changes."""
    targets = {t: pol.tier(t)["target"] for t in TIERS}
    targets["app"] = float(pol.get("allowance", {}).get("app_target", 0.995))
    window = pol.window_minutes()
    exit_yellow = float(pol.get("allowance", {}).get("standard_yellow_exit", 0.60))
    floor = float(pol.get("allowance", {}).get("min_allowed_bad", 0))
    computed = compute(store.doc(DOC).get("buckets", []), targets, window, min_allowed_bad=floor)
    changes = []

    def fn(cur):
        changes.clear()
        for t, v in computed.items():
            old = (cur.get(t) or {}).get("mode", "green")
            new = next_mode("standard" if t == "app" else t, old, v["allowance"], pol["tiers"], exit_yellow)
            entry = {**v, "mode": new, "since": (cur.get(t) or {}).get("since") if new == old else iso()}
            if new != old:
                changes.append({"tier": t, "from": old, "to": new, "allowance": v["allowance"]})
            cur[t] = entry
        cur["updated_at"] = iso()
        return cur

    store.update("modes", fn)
    return list(changes)
