import copy

from vallum import cards as cardlib
from vallum import config


def base():
    return cardlib.load_from_dir(config.ROOT)


def test_base_cards_are_valid():
    cs = base()
    cat = config.load_local("catalog.yaml")
    assert set(cs.cards) == {"redirect", "links", "health", "status-page"}
    for name, card in cs.cards.items():
        assert cardlib.validate(card, cat, name) == [], name
    assert cs.service["promises"]


def test_missing_required_type_gets_a_plain_hint():
    cat = config.load_local("catalog.yaml")
    card = copy.deepcopy(base().cards["links"])
    del card["promises"]["security"]
    problems = cardlib.validate(card, cat)
    assert any("needs a security promise" in p and "Add one like" in p for p in problems)


def test_never_allowed_and_unknown_kinds():
    cat = config.load_local("catalog.yaml")
    card = copy.deepcopy(base().cards["links"])
    card["kind"] = "payments"
    assert any("never allowed" in p for p in cardlib.validate(card, cat))
    card["kind"] = "teleport"
    assert any("not in the catalog" in p for p in cardlib.validate(card, cat))


def test_loosening_is_detected():
    old = base()
    new = copy.deepcopy(old)
    assert cardlib.loosenings(old, new) == []
    new.cards["redirect"]["promises"]["speed"][0]["measure"] = "p95(latency, route='GET /r/{code}') < 400ms"
    new.cards["redirect"]["tier"] = "standard"
    new.cards["links"]["promises"]["security"].pop()
    new.cards["health"]["promises"]["speed"][0]["severity"] = "minor"
    found = " | ".join(cardlib.loosenings(old, new))
    assert "limit loosened from 150 to 400" in found
    assert "tier lowered from core to standard" in found
    assert "link-http-only was deleted" in found
    assert "severity lowered" in found


def test_tightening_is_fine():
    old = base()
    new = copy.deepcopy(old)
    new.cards["redirect"]["promises"]["speed"][0]["measure"] = "p95(latency, route='GET /r/{code}') < 100ms"
    new.cards["links"]["promises"]["behavior"][1]["where"].append("live")
    assert cardlib.loosenings(old, new) == []


def test_blast_radius_and_effective_tier():
    cs = base()
    cat = config.load_local("catalog.yaml")
    touched = cardlib.blast_radius(["app/features/redirect.py", "README.md"], cs.cards)
    assert touched == {"redirect"}
    new_card = {"kind": "new-endpoint", "tier": "experimental"}
    assert cardlib.effective_tier([new_card], cat, set(), cs.cards, ["app/features/qr.py"], []) == "standard"
    assert cardlib.effective_tier([new_card], cat, touched, cs.cards, ["app/features/redirect.py"], []) == "core"
    assert cardlib.effective_tier([new_card], cat, set(), cs.cards, ["app/main.py"], ["app/main.py"]) == "core"
