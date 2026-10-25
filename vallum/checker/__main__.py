"""MR checkpoint: every `mr` promise on a localhost copy of the branch, plus the fail-first check
(new behavior promises must fail on main and pass on the branch, in the same job).

    python -m vallum.checker [--out reports/checker.json] [--no-fail-first]
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from vallum import cards as cardlib
from vallum import config
from vallum.checker import core, dsl
from vallum.checker.runner import fetch_main_source, local_app, wait_for_port
from vallum.gitlab import GitLab

API_KEY = os.environ.get("CHECKER_API_KEY", "test-key")


def new_behavior_promises(branch: cardlib.CardSet, main: cardlib.CardSet) -> list[cardlib.Promise]:
    old_ids = {(p.feature, p.id) for p in main.promises()}
    return [p for p in branch.promises(where="mr")
            if (p.feature, p.id) not in old_ids and p.type == "behavior" and p.style == "steps"]


FIX_KINDS = ("bug-fix", "security-fix", "performance")


def guards_allowed(cards, rows) -> bool:
    """Guards alone prove nothing new, so they are only enough on a fix card (the bug is not a behavior)."""
    features = {r["feature"] for r in rows}
    kinds = {c.get("kind") for c in cards.cards.values() if c.get("feature") in features}
    return bool(kinds) and kinds <= set(FIX_KINDS)


def run(out: Path, fail_first: bool = True) -> dict:
    pol = config.policy(GitLab())
    retry_s = float(pol["judge"]["step_retry_s"])
    branch = cardlib.load_from_dir(config.ROOT)
    promises = branch.promises(where="mr")
    env = {"APP_API_KEY": API_KEY}
    emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if emulator:
        host, _, port = emulator.partition(":")
        if not wait_for_port(host, int(port or 8080), timeout=120):
            raise SystemExit(f"Firestore emulator at {emulator} never came up")
    else:
        env["APP_STORE"] = "memory"

    results = []
    reports_dir = config.ROOT
    out.parent.mkdir(parents=True, exist_ok=True)
    with local_app(config.ROOT, 8002, env, log_path=out.parent / "app-branch.log") as base:
        with core.client_for(base) as client:
            metric = [p for p in promises if p.style == "measure"]
            routes = {metric_route(p) for p in metric}
            samples = core.local_load(client, routes, int(pol["judge"]["mr_samples"]), API_KEY) if routes else []
            for p in promises:
                if p.style == "steps":
                    outcome = core.run_step_promise(client, p, API_KEY, retry_s)
                elif p.style == "measure":
                    outcome = core.judge_single(p, samples, int(pol["judge"]["mr_samples"]))
                elif p.style == "static":
                    outcome = core.run_static_promise(p, config.ROOT)
                elif p.style == "scan":
                    outcome = core.run_scan_promise(p, reports_dir)
                else:
                    outcome = {"verdict": "broken", "detail": "promise has no check style"}
                results.append(core.result_row(p, outcome))

    ff_rows, ff_ok, ff_note = [], True, "no new behavior promises"
    if fail_first:
        gl = GitLab()
        main_cards = cardlib.load_from_gitlab(gl, os.environ.get("CI_DEFAULT_BRANCH", "main"))
        fresh = new_behavior_promises(branch, main_cards)
        if fresh:
            ff_note = f"{len(fresh)} new behavior promise(s)"
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    main_src = fetch_main_source(gl, Path(tmp))
                    with local_app(main_src, 8001, env, log_path=out.parent / "app-main.log") as main_base:
                        with core.client_for(main_base) as main_client:
                            for p in fresh:
                                passed, detail = dsl.run_steps(main_client, p.data["steps"], API_KEY)
                                ok_on_branch = next((r["verdict"] == "kept" for r in results
                                                     if r["id"] == p.id and r["feature"] == p.feature), False)
                                guard = bool(p.data.get("guard"))
                                row = {"id": p.id, "feature": p.feature, "failed_on_main": not passed,
                                       "passed_on_branch": ok_on_branch, "main_detail": detail, "guard": guard}
                                # a regression guard must hold on both; any other new promise must fail first
                                row["ok"] = (passed and ok_on_branch) if guard else (not passed and ok_on_branch)
                                ff_rows.append(row)
                except RuntimeError as exc:
                    ff_note = f"could not run main for the fail-first check: {exc}"
                    ff_ok = False
            ff_ok = ff_ok and all(r["ok"] for r in ff_rows)
            only_guards = ff_rows and all(r["guard"] for r in ff_rows)
            if only_guards and not guards_allowed(branch, ff_rows):
                ff_ok = False
                ff_note += ": only guards; a new feature needs at least one promise that fails on main"

    summary = core.summarize(results)
    report = {
        "checkpoint": "mr",
        "sha": os.environ.get("CI_COMMIT_SHA"),
        "pipeline_id": os.environ.get("CI_PIPELINE_ID"),
        "promises": results,
        "fail_first": {"ok": ff_ok, "note": ff_note, "promises": ff_rows},
        "summary": summary,
        "ok": summary["blocking"] == 0 and ff_ok,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def metric_route(p: cardlib.Promise) -> str:
    from vallum.checker.measure import parse_measure

    return parse_measure(p.data["measure"]).route


def print_report(report: dict):
    marks = {"kept": "✅", "broken": "❌", "skipped": "➖", "collecting": "⏳"}
    print(f"\nPromise checker · {report['summary']}")
    for r in report["promises"]:
        print(f"  {marks.get(r['verdict'], '?')} [{r['severity']:7}] {r['feature']}/{r['id']}: {r['detail']}")
    ff = report["fail_first"]
    print(f"\nFail-first: {'✅' if ff['ok'] else '❌'} {ff['note']}")
    for r in ff["promises"]:
        print(f"  {'✅' if r['ok'] else '❌'} {r['feature']}/{r['id']}: fails on main={r['failed_on_main']}, passes on branch={r['passed_on_branch']}")
    print(f"\nResult: {'PASS' if report['ok'] else 'FAIL'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports/checker.json")
    parser.add_argument("--no-fail-first", action="store_true")
    args = parser.parse_args()
    report = run(Path(args.out), fail_first=not args.no_fail_first)
    print_report(report)
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
