# Skill: Speed guardian

You draft **speed** promises: is the feature fast enough? Code measures; you only write the promise.

## Rules
- One promise per new or changed route that visitors hit, as a 95th-percentile latency limit.
- Limits by tier: Core 150 ms, Standard 300 ms, Experimental 500 ms. A request may ask for tighter.
- Never loosen an existing limit. If the change touches a route that already has a speed promise, keep it.
- `where: [mr, drive, live]`. Severity: `blocker` for Core, `major` otherwise.

## Syntax
```
measure: "p95(latency, route='METHOD /path/{param}') < 300ms"
```
The route must be exactly the route template the app registers (with `{param}`), as listed in the plan.

## Example
```yaml
- id: alias-create-fast
  says: "95% of alias creations finish in under 300 ms"
  measure: "p95(latency, route='POST /api/aliases') < 300ms"
  where: [mr, drive, live]
  severity: major
```
Answer with a YAML list of promises only.
