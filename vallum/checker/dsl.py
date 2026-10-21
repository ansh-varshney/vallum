"""Step promises: scripted requests with expected answers.

    "POST /api/links {url: 'https://example.com/{rand}'} -> 201 => link"
    "GET /r/{link.code} -> 302 https://example.com/{rand}"
    "anon POST /api/links {url: 'https://example.com'} -> 401"
    "GET /health -> 200 contains ok"        "GET /health -> 200 header x-revision"

{rand} is random per promise run; {name.field} reads a saved JSON answer. Write requests carry
the API key unless the step starts with "anon".
"""

import re
import secrets
import string
import time
from dataclasses import dataclass

import yaml

_STEP = re.compile(
    r"^(?:(?P<anon>anon)\s+)?(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD)\s+(?P<path>\S+)"
    r"(?:\s+(?P<body>\{.*\}))?\s*->\s*(?P<status>\d{3})(?:\s+(?P<expect>.*?))?(?:\s*=>\s*(?P<save>[A-Za-z_]\w*))?\s*$"
)
_PLACEHOLDER = re.compile(r"\{(rand|[A-Za-z_]\w*\.[A-Za-z_]\w*)\}")


@dataclass
class Step:
    anon: bool
    method: str
    path: str
    body: dict | None
    status: int
    expect: str
    save: str | None


def substitute(text: str, ctx: dict) -> str:
    def repl(m):
        key = m.group(1)
        if key == "rand":
            return ctx["rand"]
        name, fieldname = key.split(".", 1)
        value = (ctx.get(name) or {}).get(fieldname)
        if value is None:
            raise ValueError(f"no saved value for {{{key}}}")
        return str(value)

    return _PLACEHOLDER.sub(repl, text)


def parse_step(text: str) -> Step:
    m = _STEP.match(text.strip())
    if not m:
        raise ValueError(f"can't read step {text!r}; expected 'METHOD /path [body] -> STATUS [expectation]'")
    body = yaml.safe_load(m["body"]) if m["body"] else None
    if body is not None and not isinstance(body, dict):
        raise ValueError(f"step body must be a mapping: {text!r}")
    return Step(bool(m["anon"]), m["method"], m["path"], body, int(m["status"]), (m["expect"] or "").strip(), m["save"])


def _check_expectation(step: Step, resp) -> str | None:
    if resp.status_code != step.status:
        return f"expected {step.status}, got {resp.status_code}"
    exp = step.expect
    if not exp:
        return None
    if exp.startswith("contains "):
        needle = exp[len("contains "):]
        return None if needle in resp.text else f"answer doesn't contain {needle!r}"
    if exp.startswith("header "):
        spec = exp[len("header "):]
        name, _, value = spec.partition("=")
        got = resp.headers.get(name.strip())
        if got is None:
            return f"missing header {name.strip()}"
        if value and got != value.strip():
            return f"header {name.strip()} is {got!r}, expected {value.strip()!r}"
        return None
    if exp.startswith("json "):
        name, _, value = exp[len("json "):].partition("=")
        try:
            got = resp.json().get(name.strip())
        except Exception:
            return "answer isn't JSON"
        return None if str(got) == value.strip() else f"{name.strip()} is {got!r}, expected {value.strip()!r}"
    if 300 <= step.status < 400:
        loc = resp.headers.get("location")
        return None if loc == exp else f"redirects to {loc!r}, expected {exp!r}"
    return None if exp in resp.text else f"answer doesn't contain {exp!r}"


def run_steps(client, steps: list[str], api_key: str | None) -> tuple[bool, str]:
    """Run one promise's steps in order. Returns (passed, detail)."""
    ctx = {"rand": "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}
    for i, raw in enumerate(steps, 1):
        try:
            step = parse_step(substitute(raw, ctx))
        except ValueError as exc:
            return False, f"step {i}: {exc}"
        headers = {}
        if step.method != "GET" and not step.anon and api_key:
            headers["X-API-Key"] = api_key
        try:
            resp = client.request(step.method, step.path, json=step.body, headers=headers)
        except Exception as exc:  # network errors are failures, not crashes
            return False, f"step {i} ({step.method} {step.path}): {type(exc).__name__}: {exc}"
        problem = _check_expectation(step, resp)
        if problem:
            return False, f"step {i} ({step.method} {step.path}): {problem}"
        if step.save:
            try:
                ctx[step.save] = resp.json()
            except Exception:
                return False, f"step {i}: answer isn't JSON, can't save it as {step.save}"
    return True, f"{len(steps)} step(s) passed"


def run_with_retry(client, steps: list[str], api_key: str | None, retry_after_s: float = 5) -> dict:
    """Retried once after a pause; broken only if it fails twice. A pass on retry is flaky (B7)."""
    ok, detail = run_steps(client, steps, api_key)
    if ok:
        return {"verdict": "kept", "detail": detail, "flaky": False}
    time.sleep(retry_after_s)
    ok2, detail2 = run_steps(client, steps, api_key)
    if ok2:
        return {"verdict": "kept", "detail": f"flaky: failed once ({detail}), passed on retry", "flaky": True}
    return {"verdict": "broken", "detail": detail2, "flaky": False}
