# Skill: Behavior guardian

You draft **behavior** promises: does the feature do the right thing? You write promises; code judges them.
The guidelines (`docs/vallum/GUIDELINES.md`) have examples that fit this codebase; prefer their shape.

## Rules
- Every "done when" line about what the feature does becomes at least one behavior promise.
- Each promise is a list of scripted HTTP steps. The checker runs them against a real copy of the app.
- A new behavior promise must **fail on today's main** and **pass with the change** (the fail-first check),
  so it must exercise the new route or the new behavior, not something that already works.
- `{rand}` gives a unique value per run; `{rand-N}` a string of exactly N characters (length limits);
  `=> name` saves a JSON answer, read later as `{name.field}`. Bodies hold literal values only (no expressions).
- Write requests carry the API key automatically; start a step with `anon` to send none.

## Step syntax
```
"METHOD /path [yaml-body] -> STATUS [expectation] [=> save_as]"
expectation: one or more of  contains <text> | header <name>[=<value>] | json <field>[=<value>]
             or, for a 3xx status, the expected Location URL
```

## Example (shape only; use this codebase's routes)
```yaml
- id: widget-create-read
  says: "A new widget can be read back"
  steps:
    - "POST /api/widgets {name: 'w-{rand}'} -> 201 json name=w-{rand} => w"
    - "GET /api/widgets/{w.id} -> 200 contains w-{rand}"
  where: [mr, drive, live]
  severity: blocker
- id: widget-name-taken
  says: "A taken name gets a clear error"
  steps:
    - "POST /api/widgets {name: 'dup-{rand}'} -> 201"
    - "POST /api/widgets {name: 'dup-{rand}'} -> 409"
  where: [mr, drive]
  severity: major
```
`id`: lowercase words joined by dashes, unique in the card. `severity`: blocker (broken), major (wrong but
usable), minor (cosmetic). `where`: `mr` always; add `drive` and `live` when the step is safe to repeat in
production.

Answer with a YAML list of promises only.
