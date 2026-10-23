import copy

import pytest
import yaml

from vallum import evidence, review_instructions, verdicts
from vallum.flows import FLOWS, FLOWS_DIR
from vallum.gate import rules
from tests.test_gate import BOT, ctx, statuses

BOTS = {"ai-vallum-ship-gitlab-ai-hackathon": "ship", "ai-vallum-inspector-gitlab-ai-hackathon": "inspect"}


def note(author, body, nid=1):
    return {"id": nid, "author": {"username": author}, "body": body, "created_at": "2026-10-22T10:00:00Z"}


REVIEW = '🛡 Vallum: **Reviewer verdict** · approve\n```json\n{"verdict": "approve", "sha": "abc", "watch_promises": []}\n```'
INSPECT = '🛡 Vallum: **Inspector verdict** · matches\n```json\n{"verdict": "matches", "sha": "abc", "fingerprint": "f1"}\n```'


def test_verdicts_only_count_from_the_right_bot():
    found = verdicts.latest([note("ai-vallum-ship-gitlab-ai-hackathon", REVIEW),
                             note("ai-vallum-inspector-gitlab-ai-hackathon", INSPECT, 2)], BOTS)
    assert found["review"]["verdict"] == "approve" and found["inspector"]["fingerprint"] == "f1"
    # a human (or another bot) pasting the marker is ignored
    forged = verdicts.latest([note("stranger", INSPECT), note("ai-vallum-ship-gitlab-ai-hackathon", INSPECT, 2)], BOTS)
    assert "inspector" not in forged
    assert verdicts.parse("🛡 Vallum: **Reviewer verdict**\n```json\n{not json}\n```", "review") is None


def test_latest_verdict_wins():
    later = REVIEW.replace('"approve"', '"changes"')
    found = verdicts.latest([note("ai-vallum-ship-gitlab-ai-hackathon", REVIEW, 1),
                             note("ai-vallum-ship-gitlab-ai-hackathon", later, 2)], BOTS)
    assert found["review"]["verdict"] == "changes"


def test_done_when_lines_from_the_feature_template():
    text = open(".gitlab/issue_templates/Feature.md", encoding="utf-8").read().replace(
        "- \n- \n", "- /r/promo goes to the right page\n- Taken names get a clear error\n")
    assert evidence.done_when_lines(text) == ["/r/promo goes to the right page", "Taken names get a clear error"]
    assert evidence.related_issue("Related to #12\n\nmore") == 12
    assert evidence.related_issue("no link") is None


def test_fingerprint_survives_a_rebase_but_not_a_change():
    a = [{"new_path": "app/features/x.py", "diff": "@@ -0,0 +1,2 @@\n+one\n+two"}]
    rebased = [{"new_path": "app/features/x.py", "diff": "@@ -3,0 +5,2 @@\n+one\n+two"}]
    changed = [{"new_path": "app/features/x.py", "diff": "@@ -0,0 +1,2 @@\n+one\n+three"}]
    assert evidence.fingerprint(a) == evidence.fingerprint(rebased) != evidence.fingerprint(changed)


def test_rule9_accepts_the_same_change_after_a_rebase():
    on = {"policy": None}
    c = ctx(reviewer_verdict="approve", reviewer_author="ai-vallum-ship", fingerprint="f1",
            inspector={"verdict": "matches", "sha": "old-sha", "fingerprint": "f1"})
    c.policy.data["gate"]["agent_verdicts_required"] = True
    assert statuses(rules.evaluate(c))[9] == "pass"
    c.inspector = {"verdict": "matches", "sha": "old-sha", "fingerprint": "other"}
    assert statuses(rules.evaluate(c))[9] == "fail"
    c.inspector = {"verdict": "gaps", "sha": "abc", "fingerprint": "f1"}
    assert statuses(rules.evaluate(c))[9] == "fail"
    c.inspector, c.reviewer_verdict = {"verdict": "matches", "sha": "abc"}, "changes"
    assert statuses(rules.evaluate(c))[9] == "fail"
    assert on


def test_incident_bot_may_only_add_learned_promises():
    files = ["promises/learned/7-step0-replay.yaml"]
    ok = ctx(author=BOT, author_is_bot=True, files=files, card=None, added_files=files, learned_writer=True)
    assert statuses(rules.evaluate(ok))[5] == "pass"
    edit = ctx(author=BOT, author_is_bot=True, files=files, card=None, added_files=[], learned_writer=True)
    assert statuses(rules.evaluate(edit))[5] == "fail"
    other_bot = ctx(author=BOT, author_is_bot=True, files=files, card=None, added_files=files, learned_writer=False)
    assert statuses(rules.evaluate(other_bot))[5] == "fail"


def test_review_instructions_match_the_rulebook():
    current = open(review_instructions.OUT, encoding="utf-8").read().replace("\r\n", "\n")
    assert current == review_instructions.render(), "run python -m vallum.review_instructions"


@pytest.mark.parametrize("key", list(FLOWS))
def test_flow_yaml_is_well_formed(key):
    """A local lint for Flow Registry v1, so mistakes are caught before publishing."""
    text = (FLOWS_DIR / FLOWS[key]["file"]).read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    assert spec["version"] == "v1" and spec["environment"] == "ambient"
    # what gitlab.com's validator refuses today (learned from the first publish)
    assert not {"name", "description"} & set(spec), "top-level name/description are not allowed"
    assert "max_cycles" not in text, "max_cycles is not allowed yet"
    assert "<!--" not in text and "<system>" not in text and "<goal>" not in text, "prompts may not contain these"
    assert FLOWS[key]["name"].startswith("Vallum")
    comps = {c["name"]: c for c in spec["components"]}
    prompts = {p["prompt_id"] for p in spec.get("prompts", [])}
    schemas = {s["schema_id"] for s in spec.get("response_schemas", [])}
    nodes = set(comps) | {"end", "abort"}
    assert spec["flow"]["entry_point"] in comps
    for name, c in comps.items():
        assert ":" not in name and "." not in name
        if c["type"] in ("AgentComponent", "OneOffComponent"):
            assert c["prompt_id"] in prompts, f"{name}: prompt {c['prompt_id']} not defined"
        if c.get("response_schema_id"):
            assert c["response_schema_id"] in schemas
        for sub in c.get("subagents", []):
            assert sub["name"] in comps and comps[sub["name"]].get("description"), f"subagent {sub['name']} needs a description"
        for tool in c.get("toolset", []):
            assert tool not in ("run_command", "start_flow"), f"{name} must not get {tool}"
    supervised = {s["name"] for c in comps.values() for s in c.get("subagents", [])}
    for r in spec["routers"]:
        assert r["from"] in comps and r["from"] not in supervised
        assert r.get("to") in nodes or all(v in nodes for v in r["condition"]["routes"].values())
    for p in spec.get("prompts", []):
        assert p["unit_primitives"] == [] and "system" in p["prompt_template"]


def test_final_json_finds_the_agents_last_verdict_in_a_trace():
    from vallum.flows import final_json

    lines = [
        '{"type": "message", "content": "thinking about {\\"verdict\\": \\"draft\\"}"}',
        '{"type": "final", "final_answer": "Here: ```json\\n{\\"verdict\\": \\"matches\\", \\"fingerprint\\": \\"f1\\", '
        '\\"trace\\": [{\\"done_when\\": \\"a {b}\\", \\"promise_id\\": \\"p\\", \\"result\\": \\"kept\\"}]}\\n```"}',
        "not json at all",
    ]
    got = final_json("\n".join(lines))
    assert got["verdict"] == "matches" and got["trace"][0]["done_when"] == "a {b}"
    assert final_json('{"x": 1}') is None


def test_inspector_has_no_tools_and_reviewer_cannot_edit_code():
    ins = yaml.safe_load((FLOWS_DIR / "inspect.yaml").read_text(encoding="utf-8"))
    agent = next(c for c in ins["components"] if c["name"] == "inspector")
    assert agent["toolset"] == []
    ship = yaml.safe_load((FLOWS_DIR / "ship.yaml").read_text(encoding="utf-8"))
    tools = {c["name"]: set(c.get("toolset", [])) for c in ship["components"]}
    assert not tools["reviewer"] & {"create_commit", "create_merge_request"}
    assert not tools["flight_director"] & {"create_commit", "create_merge_request"}
    for g in ("guardian_behavior", "guardian_speed", "guardian_reliability", "guardian_security"):
        assert tools[g] == {"get_repository_file"}
    assert copy.deepcopy(tools)


def test_dsl_handles_what_agents_actually_wrote(client):
    from vallum.checker import dsl

    # several checks in one step, existence-only json check, Location: prefix, {rand-N}
    ok, detail = dsl.run_steps(client, [
        "POST /api/links {url: 'https://example.com/{rand}'} -> 201 json url=https://example.com/{rand} json short_url json code => link",
        "GET /r/{link.code} -> 302 Location: https://example.com/{rand}",
        "POST /api/links {url: 'https://example.com/{rand-2100}'} -> 422",
    ], "test-key")
    assert ok, detail
    ok, detail = dsl.run_steps(client, ["POST /api/links {url: 'https://x'} -> 201 json nope"], "test-key")
    assert not ok and "no field 'nope'" in detail
    # an expression in a body is a broken promise with a clear reason, never a crash
    ok, detail = dsl.run_steps(client, ["POST /api/links {url: 'https://a.com/' + 2100 x chars} -> 422"], "test-key")
    assert not ok and "not a YAML mapping" in detail


def test_agent_commits_take_away_owner_exceptions():
    files = ["vallum/policy.yaml", "app/features/version.py", "promises/features/5-version.yaml"]
    human = ctx(files=files)
    assert statuses(rules.evaluate(human))[5] == "pass"
    # same MR "authored" by the owner (composite identity) but committed by a Vallum agent
    agent = ctx(files=files, agent_made=True, agent_names=["Vallum Ship"])
    report = rules.evaluate(agent)
    assert statuses(report)[5] == "fail"
    assert "made by Vallum Ship" in report["rules"][0]["detail"] and statuses(report)[1] == "pass"
