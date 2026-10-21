"""Gatekeeper report mode: a job in every MR pipeline, with no tokens and no power to merge.

    python -m vallum.gate report [--out reports/gatekeeper.json]
"""

import argparse
import json
import os
import sys
from pathlib import Path

from vallum import config
from vallum.gate import context, rules
from vallum.gitlab import GitLab


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["report"])
    parser.add_argument("--out", default="reports/gatekeeper.json")
    args = parser.parse_args()

    iid = os.environ.get("CI_MERGE_REQUEST_IID")
    if not iid:
        raise SystemExit("Report mode runs in merge request pipelines only.")
    gl = GitLab()  # deliberately no token: public reads only
    mr = gl.mr(int(iid))
    mr["sha"] = os.environ.get("CI_COMMIT_SHA", mr["sha"])
    ctx = context.build(gl, mr, "report", local_root=config.ROOT)
    report = rules.evaluate(ctx)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(rules.render(report))
    print("\nThe Merge Desk re-runs all 12 rules on main, on the exact commit it merges." if report["ok"]
          else "\nFix the failed rules above; the Merge Desk won't pick this MR up until they pass.")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
