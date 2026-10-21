"""Metric promises: parse "p95(latency, route='GET /r/{code}') < 150ms" and compute it from samples.

A sample is a dict: {"route": "GET /r/{code}", "status": 302, "ms": 41.2, "ok": True, "revision": "...", ...tags}.
"""

import math
import operator
import re
from dataclasses import dataclass

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}
_EXPR = re.compile(
    r"^\s*(?P<func>p\d{2}|avg|max|error_rate)\s*\((?P<args>.*)\)\s*(?P<cmp><=|<)\s*(?P<num>[\d.]+)\s*(?P<unit>ms|s|%)?\s*(?:#.*)?$"
)
_ROUTE = re.compile(r"route\s*=\s*'([^']*)'")
_FILTER = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*(>=|<=|==|>|<)\s*([\d.]+)\s*$")


@dataclass(frozen=True)
class Measure:
    func: str
    route: str
    filters: tuple
    comparator: str
    limit: float          # milliseconds for latency, a fraction (0.005) for error_rate
    text: str

    @property
    def kind(self) -> str:
        return "reliability" if self.func == "error_rate" else "speed"

    def unit_value(self, value: float) -> str:
        if value is None:
            return "n/a"
        return f"{value * 100:.2f}%" if self.func == "error_rate" else f"{value:.0f} ms"


def parse_measure(text: str) -> Measure:
    m = _EXPR.match(text)
    if not m:
        raise ValueError(f"can't read metric promise: {text!r}")
    func, args = m["func"], m["args"]
    route_m = _ROUTE.search(args)
    route = route_m.group(1) if route_m else "*"
    rest = _ROUTE.sub("", args)
    filters = []
    for part in rest.split(","):
        part = part.strip()
        if not part or part == "latency":
            continue
        fm = _FILTER.match(part)
        if not fm:
            raise ValueError(f"can't read filter {part!r} in {text!r}")
        filters.append((fm[1], fm[2], float(fm[3])))
    value = float(m["num"])
    unit = m["unit"] or ("%" if func == "error_rate" else "ms")
    if func == "error_rate":
        limit = value / 100 if unit == "%" else value
    else:
        limit = value * 1000 if unit == "s" else value
    return Measure(func, route, tuple(filters), m["cmp"], limit, text)


def select(samples: list[dict], measure: Measure, revision: str | None = None) -> list[dict]:
    out = []
    for s in samples:
        if revision is not None and s.get("revision") != revision:
            continue
        if measure.route != "*" and s.get("route") != measure.route:
            continue
        if any(name not in s or not OPS[op](s[name], val) for name, op, val in measure.filters):
            continue
        out.append(s)
    return out


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def compute(measure: Measure, samples: list[dict]) -> float | None:
    if not samples:
        return None
    if measure.func == "error_rate":
        return sum(1 for s in samples if not s.get("ok", False)) / len(samples)
    latencies = [s["ms"] for s in samples if s.get("ms") is not None]
    if measure.func == "avg":
        return sum(latencies) / len(latencies) if latencies else None
    if measure.func == "max":
        return max(latencies) if latencies else None
    return percentile(latencies, float(measure.func[1:]))


def within(measure: Measure, value: float) -> bool:
    return OPS[measure.comparator](value, measure.limit)
