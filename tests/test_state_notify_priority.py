import pytest

from vallum import notify, priority
from vallum.state import Abort, MemoryState, Store


def store():
    return Store(MemoryState())


def test_release_slot_is_exclusive():
    s = store()
    s.claim_release(("idle",), slot="merged", release_id="r-0001")
    with pytest.raises(Abort):
        s.claim_release(("idle",), slot="merged", release_id="r-0002")
    s.claim_release(("merged",), slot="driving")
    assert s.release()["slot"] == "driving" and s.release()["release_id"] == "r-0001"


def test_release_ids_count_up():
    s = store()
    assert [s.next_release_id() for _ in range(3)] == ["r-0001", "r-0002", "r-0003"]


def test_circuit_breaker_trips_on_the_third_rollback():
    s = store()
    assert s.record_rollback(6, 3) is False
    assert s.record_rollback(6, 3) is False
    assert s.record_rollback(6, 3) is True
    assert s.doc("meta")["safe_mode"] is True
    assert s.record_rollback(6, 3) is False  # already on


def test_events_are_capped_newest_first():
    s = store()
    for i in range(40):
        s.event("step", f"event {i}")
    events = s.doc("recent")["events"]
    assert len(events) == 30 and events[0]["summary"] == "event 39"


def test_sanctioned_commits():
    s = store()
    s.sanction("abc", mr_iid=3)
    assert s.sanctioned("abc")["mr_iid"] == 3
    s.unsanction("abc")
    assert s.sanctioned("abc") is None


def test_status_comment_is_rendered_from_milestones():
    s = store()
    s.add_milestone("merge_requests-4", "Gatekeeper 12/12", "done", kind="merge_requests", iid=4, title="Add version", tier="standard")
    s.add_milestone("merge_requests-4", "10%", "running")
    s.add_milestone("merge_requests-4", "10%", "done", note="240 visits")
    tl = s.set_timeline("merge_requests-4", now="Watching.")
    body = notify.render_status(tl)
    assert body.startswith("🛡 Vallum: **Live status** · !4 Add version")
    assert "tier **Standard**" in body and notify.STATUS_MARK in body
    assert "✅ 10%" in body and "(240 visits)" in body and body.count("10%") == 1


def test_alert_has_every_section():
    body = notify.render_alert("Rolled back", "dev", "a", "b", "c", "d", "No.")
    for part in ["What happened", "Impact", "What Vallum did", "What happens next", "Do you need to act?", "@dev"]:
        assert part in body


def test_priority_formula_examples_from_the_prd():
    assert priority.score("feature", "many", "none", "standard", False)[:2] == ("P2", 35)
    assert priority.score("bug-fix", "all", "none", "core", False)[:2] == ("P1", 70)
    assert priority.score("experiment", "few", "none", "experimental", False)[:2] == ("P3", 5)
    assert priority.score("security-fix", "few", "none", "experimental", False)[0] == "P0"


def test_priority_override_aging_and_order():
    assert priority.label_override(["vallum", "priority::P1"]) == "P1"
    assert priority.aged("P3", "2026-10-19T00:00:00+00:00") == "P1"
    assert priority.aged("P2", None) == "P2"
    items = [("P2", "2026-10-21T10:00:00"), ("P0", "2026-10-21T12:00:00"), ("P2", "2026-10-21T09:00:00")]
    ordered = sorted(items, key=lambda x: priority.queue_key(*x))
    assert ordered[0][0] == "P0" and ordered[1][1] == "2026-10-21T09:00:00"
