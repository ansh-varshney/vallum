"""What the public status page shows (B27): one summary document, written by code in main pipelines and read
by the app with its read-only identity. Only public facts (FR-STAT-2): the Feature Cards of the live code
(names, tiers, promise ids and their plain English), live-check and test-drive results, the serving revision
and links into the public project. Never tokens, keys, agent sessions or anything from `vallum_secrets`."""

from vallum.cards import card_promises
from vallum.config import iso

DOC = "public"


def summarize(cardset, live: dict, revision: str | None, commit: str | None) -> dict:
    """`live` maps 'feature/id' to 'kept' or 'broken' for the promises the Live Patrol checked in this run."""
    features = {}
    for name, card in sorted(cardset.cards.items()):
        promises = [{"id": p.id, "type": p.type, "severity": p.severity, "says": p.says, "source": p.source,
                     "where": p.where, "live": live.get(f"{p.feature}/{p.id}")} for p in card_promises(card)]
        features[card.get("feature") or name] = {
            "tier": card.get("tier"), "priority": card.get("priority"), "kind": card.get("kind"),
            "flag": card.get("flag"), "routes": list((card.get("owns") or {}).get("routes") or []),
            "promises": promises}
    return {"features": features, "checked_at": iso(), "revision": revision, "commit": commit}


def record_live(store, cardset, live: dict, revision: str | None, commit: str | None, **links):
    summary = {**summarize(cardset, live, revision, commit), **links}
    store.update(DOC, lambda cur: {**cur, **summary})
    return summary


def judged(release_id: str, steps: list[dict]) -> dict:
    """The latest test-drive result of each promise in a promoted release (a later step overrides an earlier one)."""
    out = {}
    for st in steps:
        for r in st.get("rows", []) or []:
            out[f"{r.get('feature')}/{r.get('id')}"] = {
                "release": release_id, "step": st.get("step"), "verdict": r.get("verdict"),
                "detail": str(r.get("detail") or "")[:160]}
    return out


def record_judged(store, release_id: str, steps: list[dict]):
    results = judged(release_id, steps)
    store.update(DOC, lambda cur: {**cur, "judged": {**(cur.get("judged") or {}), **results}, "judged_at": iso()})
    return results
