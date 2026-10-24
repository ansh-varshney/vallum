"""Feature Cards: loading, validation, effective tier, blast radius and the one-way rule."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

from vallum.config import ROOT, VALLUM_DIR

TYPES = ["behavior", "speed", "reliability", "security", "data", "compatibility", "cost", "operability"]
TIER_RANK = {"experimental": 1, "standard": 2, "core": 3}
SEVERITY_RANK = {"minor": 1, "major": 2, "blocker": 3}
CARDS_DIR = "promises/features"


@dataclass
class Promise:
    id: str
    type: str
    data: dict
    feature: str            # feature name, or "service" / "learned"
    source: str             # card, watch, learned, service

    @property
    def style(self) -> str:
        for key in ("steps", "measure", "scan", "static"):
            if key in self.data:
                return key
        return "unknown"

    @property
    def where(self) -> list[str]:
        return list(self.data.get("where", []))

    @property
    def severity(self) -> str:
        return self.data.get("severity", "blocker")

    @property
    def says(self) -> str:
        return self.data.get("says", "")


@dataclass
class CardSet:
    cards: dict = field(default_factory=dict)      # feature -> card dict
    paths: dict = field(default_factory=dict)      # feature -> repo path
    service: dict = field(default_factory=dict)
    learned: list = field(default_factory=list)
    errors: list = field(default_factory=list)     # (path, message) for files that failed to parse

    def promises(self, where: str | None = None, features: set | None = None, include_service=True) -> list[Promise]:
        out = []
        for name, card in self.cards.items():
            if features is not None and name not in features:
                continue
            out.extend(card_promises(card))
        if include_service:
            for p in self.service.get("promises", []) or []:
                out.append(Promise(p["id"], p.get("type", "behavior"), p, "service", "service"))
            for p in self.learned:
                out.append(Promise(p["id"], p.get("type", "behavior"), p, "learned", "learned"))
        if where:
            out = [p for p in out if where in p.where]
        return out


def card_promises(card: dict) -> list[Promise]:
    name = card.get("feature", "?")
    out = []
    for ptype in TYPES:
        for p in (card.get("promises") or {}).get(ptype, []) or []:
            out.append(Promise(p["id"], ptype, p, name, "card"))
    for p in card.get("watch", []) or []:
        out.append(Promise(p["id"], p.get("type", "behavior"), p, name, "watch"))
    for p in card.get("learned", []) or []:
        out.append(Promise(p["id"], p.get("type", "behavior"), p, name, "learned"))
    return out


def parse_card_text(text: str):
    return yaml.safe_load(text) or {}


def _card_key(cs: CardSet, card: dict, stem: str) -> str:
    """Cards are keyed by feature name. A second card for the same feature (for example a bug-fix card next to
    the feature's own card) keeps its own key, so neither card's promises are silently dropped."""
    name = card.get("feature", stem) if isinstance(card, dict) else stem
    return name if name not in cs.cards else f"{name}@{stem}"


def load_from_dir(root: Path = ROOT) -> CardSet:
    cs = CardSet()
    for path in sorted((root / CARDS_DIR).glob("*.yaml")):
        rel = path.relative_to(root).as_posix()
        try:
            card = parse_card_text(path.read_text(encoding="utf-8"))
            key = _card_key(cs, card, path.stem)
            cs.cards[key] = card
            cs.paths[key] = rel
        except yaml.YAMLError as exc:
            cs.errors.append((rel, f"is not valid YAML: {exc}"))
    service = root / "promises" / "service.yaml"
    if service.exists():
        cs.service = parse_card_text(service.read_text(encoding="utf-8"))
    learned_dir = root / "promises" / "learned"
    for path in sorted(learned_dir.glob("*.yaml")) if learned_dir.exists() else []:
        data = parse_card_text(path.read_text(encoding="utf-8"))
        cs.learned.extend(data if isinstance(data, list) else [data])
    return cs


def load_from_gitlab(gitlab, ref: str) -> CardSet:
    cs = CardSet()
    for entry in gitlab.tree(CARDS_DIR, ref=ref):
        if entry["type"] != "blob" or not entry["path"].endswith(".yaml"):
            continue
        try:
            card = parse_card_text(gitlab.file_raw(entry["path"], ref))
            key = _card_key(cs, card, Path(entry["path"]).stem)
            cs.cards[key] = card
            cs.paths[key] = entry["path"]
        except yaml.YAMLError as exc:
            cs.errors.append((entry["path"], f"is not valid YAML: {exc}"))
    text = gitlab.file_raw_or_none("promises/service.yaml", ref)
    if text:
        cs.service = parse_card_text(text)
    for entry in gitlab.tree("promises/learned", ref=ref):
        if entry["type"] == "blob" and entry["path"].endswith(".yaml"):
            data = parse_card_text(gitlab.file_raw(entry["path"], ref))
            cs.learned.extend(data if isinstance(data, list) else [data])
    return cs


_schema_cache = {}


def schema() -> dict:
    if "card" not in _schema_cache:
        _schema_cache["card"] = json.loads((VALLUM_DIR / "schema" / "card.json").read_text(encoding="utf-8"))
    return _schema_cache["card"]


HINTS = {
    "data": "Add one like: \"No visitor IP addresses in logs\".",
    "compatibility": "Add one like: \"Links made before this change still redirect\".",
    "behavior": "Add one like: \"/r/promo goes to the right page\", written as steps.",
    "speed": "Add one like: \"95% of requests finish under 150 ms\".",
    "reliability": "Add one like: \"Under 0.5% of requests fail\".",
    "security": "Add one like: \"Creating a link without the API key is refused\".",
    "cost": "Add one like: \"Image under 200 MB\".",
    "operability": "Add one like: \"Has a kill switch and a health check\".",
}


def validate(card: dict, catalog: dict, label: str = "card") -> list[str]:
    """Plain-English problems with a card (FR-CARD-2, FR-CARD-3, FR-HELP-4)."""
    problems = []
    validator = jsonschema.Draft202012Validator(schema())
    for err in sorted(validator.iter_errors(card), key=lambda e: list(e.path)):
        where = ".".join(str(p) for p in err.path) or "the card"
        problems.append(f"{label}: {where}: {err.message}")
    kind = card.get("kind")
    kinds = catalog.get("kinds", {})
    if kind in catalog.get("never_allowed", []):
        problems.append(f"{label}: kind '{kind}' is never allowed; a human must do this change.")
    elif kind not in kinds:
        problems.append(f"{label}: kind '{kind}' is not in the catalog (vallum/catalog.md).")
    else:
        present = {t for t in TYPES if (card.get("promises") or {}).get(t)}
        for needed in kinds[kind]["requires"]:
            if needed not in present:
                problems.append(f"{label}: a {kind} change needs a {needed} promise. {HINTS.get(needed, '')}".strip())
    seen = set()
    for p in card_promises(card):
        if p.id in seen:
            problems.append(f"{label}: promise id '{p.id}' is used twice.")
        seen.add(p.id)
    return problems


def owner_of(path: str, cards: dict) -> list[str]:
    names = []
    for name, card in cards.items():
        for owned in (card.get("owns") or {}).get("files", []) or []:
            if path == owned or (owned.endswith("/") and path.startswith(owned)):
                names.append(name)
    return names


def blast_radius(changed_files: list[str], cards: dict) -> set[str]:
    hit = set()
    for path in changed_files:
        hit.update(owner_of(path, cards))
    return hit


def effective_tier(subject_cards: list[dict], catalog: dict, touched_features: set[str], all_cards: dict,
                   changed_files: list[str], core_paths: list[str]) -> str:
    """Highest of: requested tier, catalog default, tier of any feature whose code is touched (FR-TIER-2)."""
    rank = 0
    for card in subject_cards:
        rank = max(rank, TIER_RANK.get(card.get("tier"), 0))
        default = catalog.get("kinds", {}).get(card.get("kind"), {}).get("default_tier")
        rank = max(rank, TIER_RANK.get(default, 0))
    for name in touched_features:
        rank = max(rank, TIER_RANK.get(all_cards.get(name, {}).get("tier"), 0))
    if any(f in core_paths for f in changed_files):
        rank = 3
    for name, r in TIER_RANK.items():
        if r == max(rank, 1):
            return name
    return "standard"


def _limit(promise: dict):
    from vallum.checker.measure import parse_measure

    try:
        m = parse_measure(promise["measure"])
        return (m.func, m.route, tuple(sorted(m.filters))), m.limit
    except Exception:
        return None, None


def loosenings(old: CardSet, new: CardSet) -> list[str]:
    """Every way the new cards are weaker than the old ones (FR-CARD-5, FR-HALL-5)."""
    found = []
    old_p = {(p.feature, p.id): p for p in old.promises(include_service=True) if p.source != "watch"}
    new_p = {(p.feature, p.id): p for p in new.promises(include_service=True)}
    for key, op in old_p.items():
        np = new_p.get(key)
        label = f"{key[0]}/{key[1]}"
        if np is None:
            found.append(f"promise {label} was deleted")
            continue
        if SEVERITY_RANK.get(np.severity, 0) < SEVERITY_RANK.get(op.severity, 0):
            found.append(f"promise {label} severity lowered from {op.severity} to {np.severity}")
        missing = set(op.where) - set(np.where)
        if missing:
            found.append(f"promise {label} no longer checked at {', '.join(sorted(missing))}")
        if op.style != np.style:
            found.append(f"promise {label} changed its check style from {op.style} to {np.style}")
        elif op.style == "measure":
            ok_key, ok_lim = _limit(op.data)
            nk_key, nk_lim = _limit(np.data)
            if ok_key != nk_key:
                found.append(f"promise {label} now measures something else")
            elif ok_lim is not None and nk_lim is not None and nk_lim > ok_lim:
                found.append(f"promise {label} limit loosened from {ok_lim:g} to {nk_lim:g}")
        elif op.style in ("steps", "static", "scan") and op.data.get(op.style) != np.data.get(np.style):
            found.append(f"promise {label} had its {op.style} changed (only the owner may rewrite a check)")
    for name, card in old.cards.items():
        new_card = new.cards.get(name)
        if new_card and TIER_RANK.get(new_card.get("tier"), 0) < TIER_RANK.get(card.get("tier"), 0):
            found.append(f"feature {name} tier lowered from {card.get('tier')} to {new_card.get('tier')}")
    return found
