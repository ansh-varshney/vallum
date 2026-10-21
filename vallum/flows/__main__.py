"""Publish Vallum's flows from the repo to the AI Catalog and enable them in this project.

    python -m vallum.flows sync
"""

import sys

from vallum.flows import FLOWS, Flows
from vallum.gitlab import GitLab
from vallum.state import Store

TRIGGERS_Q = """query($path: ID!) { project(fullPath: $path) { aiFlowTriggers {
  nodes { id description eventTypes filter active aiCatalogItemConsumer { id item { name } } user { username } } } } }"""


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    gl = GitLab.from_env(require_token=True)
    flows = Flows(gl, Store.from_env())
    if cmd != "sync":
        raise SystemExit(f"unknown command {cmd}")
    failed = False
    for key in FLOWS:
        try:
            r = flows.publish(key)
            print(f"{key:9} {r['action']:9} item={r['item_id']} consumer={r['consumer_id']} bot=@{r['service_account']} "
                  f"triggers={[t.get('eventTypes') for t in r.get('triggers') or []]}")
        except Exception as exc:
            failed = True
            print(f"{key:9} FAILED: {exc}")
    try:
        import os

        path = os.environ.get("CI_PROJECT_PATH", "gitlab-ai-hackathon/transcend-october-2026/43503198/vallum")
        nodes = gl.graphql(TRIGGERS_Q, {"path": path})["project"]["aiFlowTriggers"]["nodes"]
        print("\nTriggers in this project:")
        for t in nodes:
            item = ((t.get("aiCatalogItemConsumer") or {}).get("item") or {}).get("name")
            print(f"  {t['id']} active={t.get('active')} events={t['eventTypes']} flow={item} "
                  f"bot=@{(t.get('user') or {}).get('username')} filter={t.get('filter')} · {t['description']}")
    except Exception as exc:
        print(f"(could not list triggers: {exc})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
