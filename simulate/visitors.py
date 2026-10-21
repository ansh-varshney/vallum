"""Visitor simulation (B26). There are no real customers during the hackathon, so CI jobs play the
visitors while a test drive or a patrol runs, and we say so openly. Each visit is timed and tagged
with the revision that answered (X-Revision). Nothing runs on Google Cloud between drives.

Open-loop: requests start on a fixed schedule whatever the latency, like real visitors do.
"""

import asyncio
import random
import re
import time

import httpx

_PARAM = re.compile(r"\{[^}]+\}")
MAX_IN_FLIGHT = 120


def zipf_weights(n: int, s: float = 1.1) -> list[float]:
    return [1 / (i + 1) ** s for i in range(n)]


def plan_rates(routes_needed: dict, share: float, seconds: float, base_rps: float, max_rps: float,
               mix: dict) -> dict:
    """Requests per second per route so each judged route collects its sample on the new revision
    (share of traffic) within the watch time, on top of a realistic background mix."""
    usable = max(seconds, 1.0)
    rates = {route: base_rps * weight for route, weight in mix.items()}
    for route, need in routes_needed.items():
        if route == "*":
            continue
        want = need * 1.3 / (max(share, 0.001) * usable)
        rates[route] = max(rates.get(route, 0.0), want)
    total = sum(rates.values())
    if total > max_rps:
        rates = {r: v * max_rps / total for r, v in rates.items()}
    return {r: round(v, 2) for r, v in rates.items() if v > 0}


class Visitors:
    def __init__(self, base_url: str, codes: list[str], api_key: str | None, tags: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.codes = codes or ["missing"]
        self.weights = zipf_weights(len(self.codes))
        self.api_key = api_key
        self.samples: list[dict] = []
        self.tags = tags or {}
        self._start = time.monotonic()

    def _path(self, route: str) -> tuple[str, str, dict]:
        method, _, path = route.partition(" ")
        extra = {}
        if "{" in path:
            if path.startswith("/r/"):
                idx = random.choices(range(len(self.codes)), weights=self.weights)[0]
                extra["popular"] = 1 if idx < max(1, len(self.codes) // 10) else 0
                code = self.codes[idx]
            else:
                code = random.choice(self.codes)
            path = _PARAM.sub(code, path)
        return method, path, extra

    async def _one(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, route: str):
        method, path, extra = self._path(route)
        async with sem:
            start = time.perf_counter()
            try:
                if method == "POST":
                    resp = await client.post(path, json={"url": f"https://example.com/sim-{random.getrandbits(40):x}"},
                                             headers={"X-API-Key": self.api_key or ""})
                else:
                    resp = await client.request(method, path)
                status, revision = resp.status_code, resp.headers.get("x-revision")
            except Exception:
                status, revision = 0, None
            ms = (time.perf_counter() - start) * 1000
        self.samples.append({"route": route, "status": status, "ms": ms, "ok": 0 < status < 500,
                             "revision": revision, "t": time.monotonic() - self._start, **extra, **self.tags})

    async def _route_loop(self, client, sem, route: str, rps: float, until: float, tasks: set):
        interval = 1.0 / rps
        nxt = time.monotonic() + random.random() * interval
        while nxt < until:
            delay = nxt - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            task = asyncio.create_task(self._one(client, sem, route))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            nxt += interval

    async def run(self, rates: dict, seconds: float, stop: asyncio.Event | None = None):
        """Send traffic for `seconds` (or until `stop` is set). Samples accumulate in self.samples."""
        limits = httpx.Limits(max_connections=MAX_IN_FLIGHT, max_keepalive_connections=MAX_IN_FLIGHT)
        sem = asyncio.Semaphore(MAX_IN_FLIGHT)
        tasks: set = set()
        until = time.monotonic() + seconds
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10, limits=limits, follow_redirects=False,
                                     headers={"User-Agent": "vallum-visitors (simulated)"}) as client:
            loops = [asyncio.create_task(self._route_loop(client, sem, r, v, until, tasks)) for r, v in rates.items()]
            if stop is not None:
                stopper = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait(loops + [stopper], return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    for t in loops:
                        t.cancel()
                else:
                    await asyncio.gather(*loops, return_exceptions=True)
                    stopper.cancel()
            else:
                await asyncio.gather(*loops)
            if tasks:
                await asyncio.wait(list(tasks), timeout=12)


def burst(base_url: str, rates: dict, seconds: float, codes: list[str], api_key: str | None) -> list[dict]:
    """Synchronous helper: run visitors for a fixed time and return the samples."""
    v = Visitors(base_url, codes, api_key)
    asyncio.run(v.run(rates, seconds))
    return v.samples


def ensure_links(base_url: str, api_key: str, have: list[str], want: int) -> list[str]:
    """Top up the persistent link set used as 'real' traffic."""
    codes = list(have)
    with httpx.Client(base_url=base_url, timeout=15) as client:
        while len(codes) < want:
            resp = client.post("/api/links", json={"url": f"https://example.com/page-{len(codes)}"},
                               headers={"X-API-Key": api_key})
            if resp.status_code != 201:
                raise RuntimeError(f"could not create simulation links: HTTP {resp.status_code}")
            codes.append(resp.json()["code"])
    return codes
