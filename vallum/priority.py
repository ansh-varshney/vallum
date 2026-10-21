"""Priority P0-P3 (B11): a fixed, explainable formula; a human label overrides it; nothing waits forever."""

from datetime import timedelta

from vallum.config import parse_iso, utcnow

KIND_POINTS = {"bug-fix": 30, "reliability": 30, "feature": 15, "dependency": 10, "config-infra": 10, "experiment": 0}
USERS_POINTS = {"all": 25, "many": 15, "few": 5}
DEADLINE_POINTS = {"24h": 20, "week": 10, "none": 0}
TIER_POINTS = {"core": 15, "standard": 5, "experimental": 0}
LEVELS = ["P0", "P1", "P2", "P3"]


def score(kind: str, users: str, deadline: str, tier: str, customer_waiting: bool) -> tuple[str, int, str]:
    """Returns (level, points, explanation)."""
    if kind in ("security-fix", "incident-fix"):
        return "P0", 100, f"{kind} is always P0"
    parts = [
        ("kind " + kind, KIND_POINTS.get(kind, 15)),
        ("users " + users, USERS_POINTS.get(users, 5)),
        ("deadline " + deadline, DEADLINE_POINTS.get(deadline, 0)),
        ("tier " + tier, TIER_POINTS.get(tier, 0)),
    ]
    if customer_waiting:
        parts.append(("customer waiting", 10))
    total = sum(p for _, p in parts)
    level = "P0" if total >= 80 else "P1" if total >= 60 else "P2" if total >= 30 else "P3"
    why = " + ".join(f"{name} {pts}" for name, pts in parts) + f" = {total}"
    return level, total, why


def label_override(labels) -> str | None:
    for label in labels or []:
        if label.startswith("priority::") and label.split("::", 1)[1] in LEVELS:
            return label.split("::", 1)[1]
    return None


def aged(level: str, waiting_since: str | None, now=None) -> str:
    """Anything waiting more than 24 hours moves up one level, up to P1 (only the rule makes P0)."""
    start = parse_iso(waiting_since)
    if not start or level in ("P0", "P1"):
        return level
    days = int(((now or utcnow()) - start) / timedelta(hours=24))
    idx = max(LEVELS.index(level) - days, 1)
    return LEVELS[idx]


def queue_key(level: str, created_at: str):
    """Queue order: priority first, then age (FR-QUEUE-2)."""
    return (LEVELS.index(level), created_at or "")
