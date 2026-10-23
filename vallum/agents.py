"""Starting agents from code (Merge Desk, test drive, Live Patrol), with one credit guard for all of them.

Agents are started the way a member would start them: code assigns the flow's bot as assignee or reviewer
with the owner's account, so the flow runs on its own service account and bills the namespace pool. The
Flows API (personal credits) is only the fallback, and the Inspector's way in (its input is the bundle).
"""

from vallum.config import iso, utcnow


def count_usage(store, key: str):
    day = utcnow().strftime("%Y-%m-%d")
    month = day[:8]
    store.update("usage", lambda cur: {**{k: v for k, v in cur.items() if k >= month},
                                       day: int(cur.get(day, 0)) + 1, f"{day}:{key}": int(cur.get(f"{day}:{key}", 0)) + 1})


def usage_ok(store, pol, notify) -> bool:
    """Credit guard (FR-WD-4): too many code-started sessions in a day turns Safe mode on."""
    day = utcnow().strftime("%Y-%m-%d")
    used = int(store.doc("usage").get(day, 0))
    cap = int(pol.get("credits", {}).get("daily_code_started_sessions", 40))
    if used < cap:
        return True
    if not store.doc("meta").get("safe_mode"):
        store.update("meta", lambda cur: {**cur, "safe_mode": True, "safe_mode_at": iso(),
                                          "safe_mode_reason": f"credit guard: {used} agent sessions started today (cap {cap})"})
        notify.alert("issues", None, "Safe mode on (credit guard)", author=pol.owners[0] if pol.owners else None,
                     what=f"Vallum started {used} agent sessions today; the daily cap is {cap}.",
                     impact="No new agent sessions or releases until a member resumes.",
                     done="Safe mode is on.", nxt="Releases in progress finish or roll back.",
                     action="Yes: check credits, then `@vallum resume`.")
    return False


def start(flows, store, pol, notify, key: str, kind: str, iid: int, goal: dict, *, as_reviewer: bool = False,
          issue_id: int | None = None) -> str | None:
    """Start one agent on an issue or MR. Returns "trigger", "api", or None if it was not started."""
    if not usage_ok(store, pol, notify):
        return None
    via = None
    if pol.get("agents", {}).get("start_via_triggers", True):
        try:
            if flows.start_by_trigger(key, kind, iid, as_reviewer=as_reviewer):
                via = "trigger"
        except Exception as exc:
            print(f"[agents] {key}: trigger start on {kind}/{iid} failed ({exc}); using the Flows API", flush=True)
    if via is None:
        try:
            flows.start(key, goal, issue_id=issue_id)
            via = "api"
        except Exception as exc:
            print(f"[agents] {key}: could not start: {exc}", flush=True)
            return None
    count_usage(store, key)
    return via
