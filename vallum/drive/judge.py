"""Judging a test-drive step (B7). Fixed rules in code turn counts into a verdict; anything unclear
is a reason to roll back.

KEPT        within limits with enough data
COLLECTING  not enough visits yet (wait up to twice the watch time)
AT_RISK     close to the limit, or still too little data at the end: rollback (safe default)
BROKEN      over the limit and at least 20% worse than the old revision: rollback
"""

from vallum.cards import Promise
from vallum.checker.measure import compute, parse_measure, select, within

KEPT, COLLECTING, AT_RISK, BROKEN, PLATFORM = "kept", "collecting", "at_risk", "broken", "platform"


def blocks(severity: str, tier: str) -> bool:
    """blocker rolls back; major rolls back in Core and Standard; minor only creates a task."""
    return severity == "blocker" or (severity == "major" and tier in ("core", "standard"))


def judge_promise(p: Promise, samples: list[dict], new_rev: str, old_rev: str | None, *, need: int,
                  worse: float, margin: float, final: bool, warmup_s: float = 0) -> dict:
    m = parse_measure(p.data["measure"])
    usable = [s for s in samples if s.get("t", 0) >= warmup_s]
    new = select(usable, m, new_rev)
    old = select(usable, m, old_rev) if old_rev else []
    vn, vo = compute(m, new), compute(m, old)
    row = {"id": p.id, "feature": p.feature, "type": p.type, "severity": p.severity, "measure": m.text,
           "new": {"value": vn, "n": len(new), "shown": m.unit_value(vn)},
           "old": {"value": vo, "n": len(old), "shown": m.unit_value(vo)}, "limit": m.limit, "needed": need}
    if len(new) < need:
        row.update(verdict=COLLECTING, detail=f"{len(new)} of {need} visits on the new revision")
        return row
    if not within(m, vn):
        if vo is not None and len(old) >= need // 2 and vn < vo * (1 + worse) and not within(m, vo):
            row.update(verdict=PLATFORM, detail=f"new {m.unit_value(vn)} but old is equally bad ({m.unit_value(vo)}): platform problem")
            return row
        row.update(verdict=BROKEN, detail=f"{m.unit_value(vn)} against a limit of {m.unit_value(m.limit)}"
                   + (f" (old revision {m.unit_value(vo)})" if vo is not None else ""))
        return row
    near = vn > m.limit * (1 - margin) if m.comparator.startswith("<") else False
    if final and near:
        row.update(verdict=AT_RISK, detail=f"{m.unit_value(vn)} is within {margin:.0%} of the limit {m.unit_value(m.limit)}")
        return row
    row.update(verdict=KEPT, detail=f"{m.unit_value(vn)} (limit {m.unit_value(m.limit)}, n={len(new)})")
    return row


def overall(rows: list[dict], tier: str, elapsed: float, watch: float, final: bool, carry: bool = False) -> str:
    blocking = [r for r in rows if blocks(r["severity"], tier)]
    if any(r["verdict"] == BROKEN for r in blocking):
        return BROKEN
    if any(r["verdict"] == AT_RISK for r in blocking):
        return AT_RISK
    if any(r["verdict"] == COLLECTING for r in blocking):
        if carry and final:
            return KEPT          # not enough visits at this small step: the next step judges it
        if elapsed >= 2 * watch:
            return AT_RISK
        return COLLECTING
    return KEPT if final or elapsed >= watch else COLLECTING


def worst(rows: list[dict]) -> dict | None:
    order = {BROKEN: 0, AT_RISK: 1, COLLECTING: 2, PLATFORM: 3, KEPT: 4}
    return min(rows, key=lambda r: order.get(r["verdict"], 5)) if rows else None
