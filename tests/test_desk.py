from vallum.desk.__main__ import Desk


def report(*fails):
    rows = [{"no": n, "status": "pass", "detail": ""} for n in range(1, 13)]
    for no, detail in fails:
        rows[no - 1].update(status="fail", detail=detail)
    return {"rules": rows}


def test_main_moving_is_transient_not_a_refusal():
    desk = Desk.__new__(Desk)
    stale = report((6, "not rebased on the latest main"))
    assert desk.only_stale(stale) and desk.only_transient(stale)
    assert desk.only_transient(report((11, "paused: core is Red")))
    broken = report((6, "not rebased on the latest main"), (7, "broken: redirect/redirect-fast"))
    assert not desk.only_stale(broken) and not desk.only_transient(broken)
