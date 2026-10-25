"""The promise checker: runs the four check styles and writes verdicts with raw numbers.
Code measures; nobody else reports a pass or a fail (FR-TYPE-4, FR-JUDGE-1)."""

import json
import re
import time
from pathlib import Path

import httpx

from vallum.cards import Promise
from vallum.checker import dsl
from vallum.checker.measure import compute, parse_measure, select, within

SEVERITY_BLOCKS = {"blocker", "major"}
_PARAM = re.compile(r"\{[^}]+\}")


def client_for(base_url: str, timeout: float = 10.0) -> httpx.Client:
    return httpx.Client(base_url=base_url, follow_redirects=False, timeout=timeout,
                        headers={"User-Agent": "vallum-checker"})


def run_step_promise(client, promise: Promise, api_key: str | None, retry_s: float) -> dict:
    return dsl.run_with_retry(client, promise.data["steps"], api_key, retry_after_s=retry_s)


def run_static_promise(promise: Promise, root: Path, build_report: dict | None = None) -> dict:
    spec = promise.data["static"]
    for check, arg in spec.items():
        if check == "file_exists":
            if not (root / arg).exists():
                return {"verdict": "broken", "detail": f"{arg} does not exist"}
        elif check in ("file_lacks", "file_contains"):
            path, text = arg
            target = root / path
            if not target.exists():
                return {"verdict": "broken", "detail": f"{path} does not exist"}
            present = text in target.read_text(encoding="utf-8")
            if check == "file_lacks" and present:
                return {"verdict": "broken", "detail": f"{path} contains {text!r}"}
            if check == "file_contains" and not present:
                return {"verdict": "broken", "detail": f"{path} lacks {text!r}"}
        elif check == "image_below_mb":
            if not build_report:
                return {"verdict": "skipped", "detail": "image size is only known after the build"}
            size_mb = build_report.get("image_bytes", 0) / 1e6
            if size_mb >= float(arg):
                return {"verdict": "broken", "detail": f"image is {size_mb:.0f} MB, limit {arg} MB", "numbers": {"image_mb": size_mb}}
        else:
            return {"verdict": "broken", "detail": f"unknown static check {check!r}"}
    return {"verdict": "kept", "detail": "static checks passed"}


SEVERITY_ORDER = ["info", "unknown", "low", "medium", "high", "critical"]


def run_scan_promise(promise: Promise, reports_dir: Path) -> dict:
    spec = promise.data["scan"]
    report = reports_dir / {"sast": "gl-sast-report.json", "secrets": "gl-secret-detection-report.json",
                            "container": "gl-container-scanning-report.json"}.get(spec.get("report", "sast"),
                                                                                  "gl-sast-report.json")
    if not report.exists():
        return {"verdict": "skipped", "detail": f"no {report.name} in this pipeline"}
    limit = SEVERITY_ORDER.index(spec.get("max_severity", "medium").lower())
    vulns = json.loads(report.read_text(encoding="utf-8")).get("vulnerabilities", [])
    if spec.get("fixable_only"):
        # a base-image finding with no fixed package yet is not something a release can act on
        vulns = [v for v in vulns if fixable(v)]
    over = [v for v in vulns if SEVERITY_ORDER.index(str(v.get("severity", "unknown")).lower()) > limit]
    which = "fixable finding(s)" if spec.get("fixable_only") else "finding(s)"
    if over:
        names = "; ".join(v.get("name") or v.get("message") or "?" for v in over[:3])
        return {"verdict": "broken", "detail": f"{len(over)} {which} above {spec.get('max_severity', 'medium')}: {names}"}
    return {"verdict": "kept", "detail": f"{len(vulns)} {which}, none above {spec.get('max_severity', 'medium')}"}


def fixable(vuln: dict) -> bool:
    solution = str(vuln.get("solution") or "").strip().lower()
    return bool(solution) and not solution.startswith("no solution")


def judge_single(promise: Promise, samples: list[dict], min_samples: int, revision: str | None = None) -> dict:
    """A metric promise judged on one version only (MR localhost, test-drive step 0)."""
    m = parse_measure(promise.data["measure"])
    chosen = select(samples, m, revision)
    need = int(promise.data.get("judge", {}).get("min_samples", min_samples))
    value = compute(m, chosen)
    numbers = {"value": value, "limit": m.limit, "samples": len(chosen), "needed": need, "shown": m.unit_value(value)}
    if len(chosen) < need:
        return {"verdict": "collecting", "detail": f"{len(chosen)} of {need} samples", "numbers": numbers}
    if within(m, value):
        return {"verdict": "kept", "detail": f"{m.unit_value(value)} (limit {m.unit_value(m.limit)}, n={len(chosen)})", "numbers": numbers}
    return {"verdict": "broken", "detail": f"{m.unit_value(value)} breaks the limit {m.unit_value(m.limit)} (n={len(chosen)})", "numbers": numbers}


def concrete_path(template_path: str, codes: list[str], i: int) -> str:
    return _PARAM.sub(lambda _m: codes[i % len(codes)], template_path)


def local_load(client, routes: set[str], per_route: int, api_key: str | None) -> list[dict]:
    """Sequential requests against a localhost copy, enough for each metric promise's sample."""
    codes = []
    for i in range(5):
        r = client.post("/api/links", json={"url": f"https://example.com/load-{i}-{time.time_ns()}"},
                        headers={"X-API-Key": api_key or ""})
        if r.status_code == 201:
            codes.append(r.json()["code"])
    codes = codes or ["missing"]
    samples = []
    expanded = set()
    for route in routes:
        if route == "*":
            expanded.update({"GET /health", "GET /r/{code}"})
        else:
            expanded.add(route)
    for route in sorted(expanded):
        method, _, path = route.partition(" ")
        if method not in ("GET", "POST"):
            continue
        # redirects: every other visit goes to one hot link (tagged popular, like the visitor simulation), so
        # promises filtered on `popular == 1` can be judged here too
        hot = path.startswith("/r/")
        for i in range(per_route * 2 if hot else per_route):
            start = time.perf_counter()
            popular = 1 if hot and i % 2 == 0 else 0
            try:
                if method == "POST":
                    resp = client.post(path, json={"url": f"https://example.com/p-{i}-{time.time_ns()}"},
                                       headers={"X-API-Key": api_key or ""})
                else:
                    resp = client.get(_PARAM.sub(codes[0], path) if popular else concrete_path(path, codes, i))
                ms = (time.perf_counter() - start) * 1000
                samples.append({"route": route, "status": resp.status_code, "ms": ms, "ok": resp.status_code < 500,
                                "revision": resp.headers.get("x-revision"), "popular": popular})
            except Exception:
                samples.append({"route": route, "status": 0, "ms": (time.perf_counter() - start) * 1000, "ok": False,
                                "revision": None, "popular": popular})
    return samples


def summarize(results: list[dict]) -> dict:
    counts = {"kept": 0, "broken": 0, "skipped": 0, "collecting": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    blocking = [r for r in results if r["verdict"] == "broken" and r["severity"] in SEVERITY_BLOCKS]
    return {**counts, "blocking": len(blocking)}


def result_row(promise: Promise, outcome: dict) -> dict:
    return {
        "id": promise.id,
        "feature": promise.feature,
        "type": promise.type,
        "style": promise.style,
        "severity": promise.severity,
        "says": promise.says,
        **outcome,
    }
