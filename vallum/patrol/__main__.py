"""Live Patrol (scheduled main pipeline). Layer 1: Watchdog, a live health check and the Ledger.
Layer 3 adds live promise checks, allowance spending and modes.

    python -m vallum.patrol
"""

import sys

import httpx

from vallum import config, gcp
from vallum.drive.cloudrun import CloudRun
from vallum.gitlab import GitLab
from vallum.notify import Notifier
from vallum.patrol.watchdog import Watchdog
from vallum.state import Store


def main():
    pol = config.policy()
    gl = GitLab.from_env(require_token=True)
    store = Store.from_env()
    notify = Notifier(gl, store, pol)
    cr = pol["cloud_run"]

    def cloudrun():
        return CloudRun(gcp.session(), cr["project"], cr["region"], cr["service"])

    actions = Watchdog(gl, store, pol, notify, cloudrun).run()
    print("Watchdog:", ", ".join(actions) if actions else "nothing to undo")

    url = config.service_url(pol)
    try:
        resp = httpx.get(url + "/health", timeout=20)
        print(f"Live check: {url}/health -> {resp.status_code} {resp.headers.get('x-revision')}")
    except httpx.HTTPError as exc:
        print(f"Live check failed: {exc}")
    notify.ledger()
    sys.exit(0)


if __name__ == "__main__":
    main()
