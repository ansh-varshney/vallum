"""Kill switches (B24) on GitLab Feature Flags (Deploy > Feature flags), the source of truth.

Code mirrors them into the state store (vallum_state/flags) on every Live Patrol and the app reads that
mirror, so the app needs no Unleash credentials and no background poller. Vallum's own flips write GitLab and
the mirror at once and take effect within seconds; a member's flip in the GitLab UI takes effect at the next
patrol. Core features have no switch (rollback is their safety net)."""

from urllib.parse import quote

from vallum.config import iso

DOC = "flags"


def wanted(cards: dict) -> dict:
    """flag name -> feature, for every card that names a switch."""
    return {c["flag"]: c.get("feature") or name for name, c in cards.items() if c.get("flag")}


def read(gl) -> dict:
    return {f["name"]: bool(f.get("active")) for f in gl.get("/projects/:id/feature_flags", per_page=100) or []}


def ensure(gl, cards: dict, current: dict) -> list[str]:
    created = []
    for flag, feature in sorted(wanted(cards).items()):
        if flag in current:
            continue
        gl.post("/projects/:id/feature_flags", json={
            "name": flag, "version": "new_version_flag", "active": True,
            "description": f"Kill switch for the {feature} feature (Vallum turns it off if a live promise breaks)",
            "strategies": [{"name": "default", "parameters": {}, "scopes": [{"environment_scope": "*"}]}]})
        current[flag] = True
        created.append(flag)
    return created


def sync(gl, store, cards: dict) -> dict:
    """Make sure every card's switch exists in GitLab, then mirror GitLab's state. Returns
    {"flags": {name: active}, "created": [...], "flipped": {name: active}} where flipped are changes made outside
    Vallum (in the GitLab UI) since the last mirror."""
    current = read(gl)
    created = ensure(gl, cards, current)
    before = (store.doc(DOC).get("flags") or {})
    flipped = {n: a for n, a in current.items() if n in before and before[n] != a}
    store.update(DOC, lambda cur: {**cur, "flags": current, "at": iso()})
    return {"flags": current, "created": created, "flipped": flipped}


def switch(gl, store, flag: str, active: bool):
    gl.put(f"/projects/:id/feature_flags/{quote(flag, safe='')}", json={"active": active})
    store.update(DOC, lambda cur: {**cur, "flags": {**(cur.get("flags") or {}), flag: active}, "at": iso()})


def off_features(cards: dict, flags: dict) -> set[str]:
    """Features whose switch is off right now: their promises are not judged while they are off."""
    return {feature for flag, feature in wanted(cards).items() if flags.get(flag) is False}


def to_switch_off(broken: list[dict], cards: dict, flags: dict) -> dict:
    """FR-FLAG-2: flag -> the broken promise, for each non-Core feature with a switch that is still on."""
    out = {}
    for b in broken:
        card = cards.get(b["promise"].feature) or {}
        flag = card.get("flag")
        if flag and card.get("tier") != "core" and flags.get(flag, True) and flag not in out:
            out[flag] = b
    return out


def enable_for_revision(store, names: list[str], revision: str):
    """A release that changes a switched-off feature is test-driven with the switch on for its own revision only;
    visitors on the old revision keep the broken feature off."""
    store.update(DOC, lambda cur: {**cur, "for_revision": {**(cur.get("for_revision") or {}),
                                                            **{n: revision for n in names}}})


def clear_for_revision(store, revision: str):
    store.update(DOC, lambda cur: {**cur, "for_revision": {n: r for n, r in (cur.get("for_revision") or {}).items()
                                                            if r != revision}})
