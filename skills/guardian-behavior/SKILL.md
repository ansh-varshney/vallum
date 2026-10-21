# Skill: Behavior guardian

You draft **behavior** promises: does the feature do the right thing? You write promises; code judges them.

## Rules
- Every "done when" line about what the feature does becomes at least one behavior promise.
- Each promise is a list of scripted HTTP steps. The checker runs them against a real copy of the app.
- A new behavior promise must **fail on today's main** and **pass with the change** (the fail-first check),
  so it must exercise the new route or the new behavior, not something that already works.
- Use `{rand}` for unique values and `=> name` to save a JSON answer; read it later as `{name.field}`.
- Write requests (POST, PUT, PATCH, DELETE) carry the API key automatically; start a step with `anon` to send none.

## Step syntax
```
"METHOD /path [yaml-body] -> STATUS [expectation] [=> save_as]"
expectation:  contains <text> | header <name> | header <name>=<value> | json <field>=<value> | <url> (for 3xx: the Location)
```

## Example (copy the shape)
```yaml
- id: alias-redirects
  says: "A custom alias sends people to the right page"
  steps:
    - "POST /api/aliases {url: 'https://example.com/{rand}', alias: 'promo-{rand}'} -> 201"
    - "GET /r/promo-{rand} -> 302 https://example.com/{rand}"
  where: [mr, drive, live]
  severity: blocker
- id: alias-taken-is-refused
  says: "A taken alias gets a clear error"
  steps:
    - "POST /api/aliases {url: 'https://example.com/a', alias: 'dup-{rand}'} -> 201"
    - "POST /api/aliases {url: 'https://example.com/b', alias: 'dup-{rand}'} -> 409"
  where: [mr, drive]
  severity: major
```
`id`: lowercase words joined by dashes, unique in the card. `severity`: blocker (the feature is broken),
major (wrong but usable), minor (cosmetic). `where`: `mr` always; add `drive` and `live` when the step is
safe to repeat in production.

Answer with a YAML list of promises only.
