"""The Gatekeeper's twelve rules (B17). Plain code, no LLM calls (FR-GATE-1).

It runs twice: as a report in every MR pipeline (no token, some rules deferred) and as the real
check inside the Merge Desk on main, where every rule must pass on the exact commit being merged."""

from dataclasses import dataclass, field

import yaml

from vallum import cards as cardlib
from vallum.config import Policy

PASS, FAIL, DEFERRED, WAIVED = "pass", "fail", "deferred", "waived"
DEVELOPER = 30

RULE_NAMES = {
    1: "Author is a project member with Developer role or higher",
    2: "Kind is in the catalog and not never-allowed",
    3: "Feature Card is valid and has every required promise type",
    4: "No promise deleted or loosened, no tier lowered, by anyone but the owner",
    5: "No change to protected paths except by the owner; deploy settings within caps",
    6: "Rebased on the latest main, and the pipeline passed on this exact commit",
    7: "Every mr promise kept, and the fail-first check passed",
    8: "No new high or critical security finding",
    9: "Reviewer approves and the Inspector says the work matches, on this commit",
    10: "Every structured claim resolves and matches",
    11: "The mode allows it (Red: only P0/P1 fixes; Standard Yellow size cap; Safe mode)",
    12: "Within the caps, and next in the release queue",
}


@dataclass
class Rule:
    no: int
    status: str
    detail: str

    @property
    def name(self) -> str:
        return RULE_NAMES[self.no]

    def as_dict(self):
        return {"no": self.no, "name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class GateContext:
    mode: str                                  # "report" or "desk"
    policy: Policy
    catalog: dict
    author: dict                               # {"id", "username"}
    changed_files: list
    main_cards: cardlib.CardSet
    branch_cards: cardlib.CardSet
    sha: str | None = None
    author_access: int | None = None           # None: couldn't check here
    author_is_bot: bool | None = None
    changed_lines: int | None = None
    added_files: list = field(default_factory=list)
    learned_writer: bool = False               # author is the Incident Commander's bot (may ADD learned promises)
    fingerprint: str | None = None             # the MR's own changes, stable across rebases
    agent_made: bool = False                   # any commit authored by a Vallum flow's service account
    housekeeping: bool = False                 # MR made by code after a release: code map and expired watch blocks only
    override_by: str | None = None             # a member used @vallum ship-anyway on this change
    agent_names: list = field(default_factory=list)
    deploy_service_text: str | None = None     # deploy/service.yaml on the branch, if changed
    rebased: bool | None = None
    pipeline_ok: bool | None = None
    pipeline_sha: str | None = None
    checker: dict | None = None
    security_findings: list | None = None      # vulnerabilities from this pipeline's scans
    security_baseline: list | None = None      # vulnerabilities already on main
    reviewer_verdict: str | None = None
    reviewer_author: str | None = None
    inspector: dict | None = None
    claims: list = field(default_factory=list)
    claim_results: dict = field(default_factory=dict)
    modes: dict | None = None
    safe_mode: bool | None = None
    priority: str = "P2"
    is_fix: bool = False
    queue_head: bool | None = None
    caps_ok: bool | None = None
    caps_detail: str = ""

    # derived -------------------------------------------------------------------
    @property
    def by_owner(self) -> bool:
        """Owner exceptions need the owner's own commits. Flows act with a composite identity, so an
        agent's MR can show the owner as its author; the commit authors tell the truth."""
        return (self.author.get("username") in self.policy.owners and self.author_is_bot is not True
                and not self.agent_made)

    def subject_cards(self) -> list[tuple[str, dict]]:
        """Cards this MR adds or changes; if none, the cards of the features whose code it touches."""
        changed = set(self.changed_files)
        subjects = [(n, c) for n, c in self.branch_cards.cards.items() if self.branch_cards.paths.get(n) in changed]
        if subjects:
            return subjects
        return [(n, self.branch_cards.cards[n]) for n in sorted(self.touched()) if n in self.branch_cards.cards]

    def touched(self) -> set:
        return cardlib.blast_radius(self.changed_files, self.branch_cards.cards) | \
            cardlib.blast_radius(self.changed_files, self.main_cards.cards)

    def app_files(self) -> list[str]:
        return [f for f in self.changed_files if f.startswith("app/")]

    def effective_tier(self) -> str:
        subjects = [c for _, c in self.subject_cards()]
        return cardlib.effective_tier(subjects, self.catalog, self.touched(), self.branch_cards.cards,
                                      self.changed_files, self.policy.get("core_paths", []))


def _is_protected(path: str, protected: list[str]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in protected)


def rule1(ctx: GateContext) -> Rule:
    if ctx.agent_made and ctx.author_access is not None and ctx.author_access >= DEVELOPER:
        return Rule(1, PASS, f"made by {', '.join(ctx.agent_names)} for @{ctx.author.get('username')} "
                             f"(access level {ctx.author_access})")
    if ctx.author_is_bot:
        # Agent bots act for the member who started them; that member is checked at the start event.
        return Rule(1, PASS, f"@{ctx.author.get('username')} is a Vallum agent account working for a member")
    if ctx.author_access is None:
        return Rule(1, DEFERRED, "membership is checked by the Merge Desk with the owner token")
    if ctx.author_access >= DEVELOPER:
        return Rule(1, PASS, f"@{ctx.author.get('username')} has access level {ctx.author_access}")
    return Rule(1, FAIL, f"@{ctx.author.get('username')} is not a project member with Developer role")


def rule2(ctx: GateContext) -> Rule:
    subjects = ctx.subject_cards()
    if not subjects:
        if ctx.app_files():
            return Rule(2, FAIL, f"app code changed ({', '.join(ctx.app_files()[:3])}) but no Feature Card covers it")
        return Rule(2, PASS, "no feature change (docs, tests or tooling only)")
    never = set(ctx.catalog.get("never_allowed", []))
    kinds = ctx.catalog.get("kinds", {})
    bad = [f"{n}: '{c.get('kind')}'" for n, c in subjects if c.get("kind") in never or c.get("kind") not in kinds]
    if bad:
        return Rule(2, FAIL, "kind not allowed: " + "; ".join(bad))
    return Rule(2, PASS, ", ".join(f"{n}: {c.get('kind')}" for n, c in subjects))


def rule3(ctx: GateContext) -> Rule:
    problems = [f"{path} {msg}" for path, msg in ctx.branch_cards.errors]
    for name, card in ctx.subject_cards():
        problems += cardlib.validate(card, ctx.catalog, label=ctx.branch_cards.paths.get(name, name))
    unowned = [f for f in ctx.app_files() if f.startswith("app/features/") and f.endswith(".py")
               and not f.endswith("__init__.py") and not cardlib.owner_of(f, ctx.branch_cards.cards)]
    problems += [f"{f} isn't owned by any Feature Card (add it to owns.files)" for f in unowned]
    if problems:
        return Rule(3, FAIL, " | ".join(problems[:6]))
    n = len(ctx.subject_cards())
    return Rule(3, PASS, f"{n} card(s) valid" if n else "no card to check")


def rule4(ctx: GateContext) -> Rule:
    found = cardlib.loosenings(ctx.main_cards, ctx.branch_cards)
    if not found:
        return Rule(4, PASS, "promises only added or tightened")
    if ctx.by_owner:
        return Rule(4, PASS, "owner change, recorded: " + "; ".join(found[:3]))
    return Rule(4, FAIL, "; ".join(found[:5]))


def _deploy_caps(ctx: GateContext) -> list[str]:
    if not ctx.deploy_service_text:
        return []
    try:
        svc = yaml.safe_load(ctx.deploy_service_text)
        ann = svc["spec"]["template"]["metadata"].get("annotations", {})
        max_scale = int(ann.get("autoscaling.knative.dev/maxScale", "1"))
        min_scale = int(ann.get("autoscaling.knative.dev/minScale", "0"))
    except Exception as exc:
        return [f"deploy/service.yaml can't be read: {exc}"]
    cap = int(ctx.policy["cloud_run"]["max_scale_cap"])
    out = []
    if max_scale > cap:
        out.append(f"maxScale {max_scale} is above the policy cap {cap}")
    if min_scale > 1:
        out.append(f"minScale {min_scale} is above 1")
    return out


def rule5(ctx: GateContext) -> Rule:
    protected = ctx.policy.get("protected_paths", [])
    hits = [f for f in ctx.changed_files if _is_protected(f, protected)]
    caps = _deploy_caps(ctx)
    if caps:
        return Rule(5, FAIL, "; ".join(caps))
    if not hits:
        return Rule(5, PASS, "no protected path changed")
    if ctx.learned_writer and all(f.startswith("promises/learned/") and f in ctx.added_files for f in hits):
        return Rule(5, PASS, f"Incident Commander adds learned promise(s) only: {', '.join(hits[:3])}")
    if ctx.by_owner:
        return Rule(5, PASS, f"owner change to protected paths, recorded: {', '.join(hits[:4])}")
    return Rule(5, FAIL, f"protected paths changed by @{ctx.author.get('username')}: {', '.join(hits[:5])}")


def rule6(ctx: GateContext) -> Rule:
    if ctx.rebased is False and ctx.mode == "report":
        # main moving under an MR is normal; the Merge Desk rebases before it merges
        return Rule(6, DEFERRED, "main has moved on; the Merge Desk rebases and re-checks before merging")
    if ctx.rebased is False:
        return Rule(6, FAIL, "not rebased on the latest main")
    if ctx.pipeline_ok is None:
        return Rule(6, DEFERRED, "rebased on main; the Merge Desk confirms the finished pipeline")
    if not ctx.pipeline_ok:
        return Rule(6, FAIL, f"pipeline did not pass on {str(ctx.sha)[:8]}")
    if ctx.pipeline_sha and ctx.sha and ctx.pipeline_sha != ctx.sha:
        return Rule(6, FAIL, f"pipeline ran on {ctx.pipeline_sha[:8]}, not on {ctx.sha[:8]}")
    return Rule(6, PASS, f"rebased; pipeline passed on {str(ctx.sha)[:8]}")


def rule7(ctx: GateContext) -> Rule:
    if ctx.checker is None:
        return Rule(7, FAIL, "no promise checker report for this commit")
    if ctx.checker.get("sha") and ctx.sha and ctx.checker["sha"] != ctx.sha:
        return Rule(7, FAIL, "the checker report is for a different commit")
    s = ctx.checker.get("summary", {})
    ff = ctx.checker.get("fail_first", {})
    if not ctx.checker.get("ok"):
        broken = [f"{r['feature']}/{r['id']}" for r in ctx.checker.get("promises", [])
                  if r["verdict"] == "broken" and r["severity"] in ("blocker", "major")]
        why = f"broken: {', '.join(broken[:4])}" if broken else f"fail-first: {ff.get('note')}"
        return Rule(7, FAIL, why)
    return Rule(7, PASS, f"{s.get('kept', 0)} kept, {s.get('broken', 0)} minor broken; fail-first: {ff.get('note')}")


def _vuln_key(v: dict) -> str:
    ids = sorted(f"{i.get('type')}:{i.get('value')}" for i in v.get("identifiers", []) if isinstance(i, dict))
    loc = v.get("location", {})
    return v.get("cve") or f"{'|'.join(ids)}@{loc.get('file')}" or v.get("id", "")


def rule8(ctx: GateContext) -> Rule:
    if ctx.security_findings is None:
        required = ctx.policy.get("security", {}).get("require_scans", False)
        return Rule(8, FAIL if required else PASS, "no scan results in this pipeline" + ("" if required else " (scans not required yet)"))
    severe = [v for v in ctx.security_findings if str(v.get("severity", "")).lower() in ("high", "critical")]
    baseline = {_vuln_key(v) for v in (ctx.security_baseline or [])}
    new = [v for v in severe if _vuln_key(v) not in baseline]
    if new:
        return Rule(8, FAIL, f"{len(new)} new high/critical finding(s): " + "; ".join(v.get("name", "?") for v in new[:3]))
    return Rule(8, PASS, f"{len(ctx.security_findings)} finding(s), no new high or critical")


def rule9(ctx: GateContext) -> Rule:
    if not ctx.policy.get("gate", {}).get("agent_verdicts_required", False):
        return Rule(9, WAIVED, "Reviewer and Inspector arrive in Layer 2 (policy: agent_verdicts_required=false)")
    if ctx.housekeeping:
        return Rule(9, WAIVED, "housekeeping by code (code map, expired watch promises): no agent review needed")
    if ctx.mode == "report":
        return Rule(9, DEFERRED, "the Merge Desk collects the Reviewer's and the Inspector's verdicts")
    if ctx.reviewer_verdict != "approve":
        return Rule(9, FAIL, f"Reviewer verdict is {ctx.reviewer_verdict or 'missing'}")
    insp = ctx.inspector or {}
    same_change = insp.get("sha") == ctx.sha or (ctx.fingerprint and insp.get("fingerprint") == ctx.fingerprint)
    if insp.get("verdict") != "matches" or not same_change:
        return Rule(9, FAIL, f"Inspector verdict is {insp.get('verdict', 'missing')} for {str(insp.get('sha'))[:8]}")
    how = "this commit" if insp.get("sha") == ctx.sha else "these exact changes (rebased since)"
    return Rule(9, PASS, f"Reviewer @{ctx.reviewer_author or '?'} approves; Inspector says matches on {how}")


def rule10(ctx: GateContext) -> Rule:
    if not ctx.claims:
        return Rule(10, PASS, "no claims to verify")
    bad = [c for c in ctx.claims if not ctx.claim_results.get(c)]
    if bad:
        return Rule(10, FAIL, f"{len(bad)} claim(s) don't resolve: {', '.join(map(str, bad[:3]))}")
    return Rule(10, PASS, f"{len(ctx.claims)} claim(s) verified")


def rule11(ctx: GateContext) -> Rule:
    if ctx.modes is None:
        return Rule(11, DEFERRED, "the Merge Desk checks modes in the state store")
    if ctx.safe_mode:
        return Rule(11, FAIL, "Safe mode is on: no new releases until the owner says @vallum resume")
    tiers = {ctx.branch_cards.cards.get(n, ctx.main_cards.cards.get(n, {})).get("tier") for n in ctx.touched()}
    tiers.add(ctx.effective_tier())
    tiers.discard(None)
    fix_ok = ctx.is_fix and ctx.priority in ("P0", "P1")
    red = sorted(t for t in tiers | {"app"} if ctx.modes.get(t, {}).get("mode") == "red")
    if red and not fix_ok and ctx.override_by:
        return Rule(11, PASS, f"{', '.join(red)} is Red, overridden by @{ctx.override_by} (recorded)")
    if red and not fix_ok:
        return Rule(11, FAIL, f"paused: {', '.join(red)} is Red; only P0/P1 fixes may touch it")
    yellow = [t for t in tiers if ctx.modes.get(t, {}).get("mode") == "yellow"]
    cap = int(ctx.policy.get("gate", {}).get("yellow_max_changed_lines", 200))
    if yellow and ctx.changed_lines is not None and ctx.changed_lines > cap:
        return Rule(11, FAIL, f"{', '.join(yellow)} is Yellow: changes are capped at {cap} lines (this one: {ctx.changed_lines})")
    if yellow and ctx.priority == "P3":
        return Rule(11, FAIL, f"{', '.join(yellow)} is Yellow: P3 work waits")
    return Rule(11, PASS, "Red fix allowed" if red else f"modes allow it ({', '.join(sorted(tiers))})")


def rule12(ctx: GateContext) -> Rule:
    if ctx.caps_ok is None or ctx.queue_head is None:
        return Rule(12, DEFERRED, "the Merge Desk checks caps and queue order")
    if not ctx.caps_ok:
        return Rule(12, FAIL, ctx.caps_detail or "a cap is exceeded")
    if not ctx.queue_head:
        return Rule(12, FAIL, "another item is ahead in the queue")
    return Rule(12, PASS, ctx.caps_detail or "next in the queue")


RULES = [rule1, rule2, rule3, rule4, rule5, rule6, rule7, rule8, rule9, rule10, rule11, rule12]


def evaluate(ctx: GateContext) -> dict:
    results = []
    for fn in RULES:
        try:
            results.append(fn(ctx))
        except Exception as exc:  # a rule that crashes fails closed
            no = RULES.index(fn) + 1
            results.append(Rule(no, FAIL, f"rule crashed: {type(exc).__name__}: {exc}"))
    passed = sum(1 for r in results if r.status in (PASS, WAIVED))
    failed = [r for r in results if r.status == FAIL]
    deferred = [r for r in results if r.status == DEFERRED]
    if ctx.mode == "desk":
        ok = passed == len(RULES)
    else:
        ok = not failed
    return {
        "mode": ctx.mode,
        "sha": ctx.sha,
        "tier": ctx.effective_tier(),
        "features": sorted(n for n, _ in ctx.subject_cards()),
        "touched": sorted(ctx.touched()),
        "score": f"{passed}/{len(RULES)}",
        "ok": ok,
        "failed": [r.no for r in failed],
        "deferred": [r.no for r in deferred],
        "rules": [r.as_dict() for r in results],
    }


def render(report: dict) -> str:
    icon = {PASS: "✅", FAIL: "❌", DEFERRED: "🕓", WAIVED: "➖"}
    lines = [f"Gatekeeper ({report['mode']}) · {report['score']} · tier {report['tier']} · "
             f"{'PASS' if report['ok'] else 'FAIL'}"]
    for r in report["rules"]:
        lines.append(f"  {icon[r['status']]} {r['no']:>2}. {r['name']}: {r['detail']}")
    return "\n".join(lines)
