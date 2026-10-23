# Reviewer notes for this repository

Extra instructions for Vallum's Reviewer in this codebase, on top of `skills/reviewer/SKILL.md`. Owner-written.

- Anything added to the redirect path (`GET /r/{code}`) must stay fast for popular links. If a change does
  work per click (counting, logging, lookups), raise a watch promise on the popular-link latency, for example
  `p95(latency, route='GET /r/{code}', popular == 1) < 150ms` at `where: [drive]`.
- URL inputs must reject whitespace, control characters and host-less URLs (see `links.py`).
- Visitor IP addresses must never be stored or logged.
