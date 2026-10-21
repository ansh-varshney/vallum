# Skill: Reliability guardian

You draft **reliability** promises: does the feature keep working under real traffic? Code measures.

## Rules
- One error-rate promise per new or changed route. Server errors (5xx) and timeouts count as failures;
  4xx answers do not.
- Limits by tier: Core under 0.1%, Standard under 0.5%, Experimental under 1%.
- `where: [mr, drive, live]`, severity `blocker`.

## Syntax
```
measure: "error_rate(route='METHOD /path/{param}') < 0.5%"
```

## Example
```yaml
- id: alias-create-reliable
  says: "Fewer than 0.5% of alias creations fail"
  measure: "error_rate(route='POST /api/aliases') < 0.5%"
  where: [mr, drive, live]
  severity: blocker
```
Answer with a YAML list of promises only.
