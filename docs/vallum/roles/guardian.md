# Guardian notes for this repository

Extra instructions for Vallum's guardians in this codebase, on top of `skills/guardian-<type>/SKILL.md`.
Owner-written.

- Everything you need is in the Flight Director's task: the plan JSON with routes, files and done-when lines.
  Answer it in one go with a YAML list of promises; don't ask questions back.
- Routes in this app: links `POST /api/links`, `GET /api/links/{code}`; redirects `GET /r/{code}` (Core);
  pages return HTML (check them with `contains <text>`).
- Use `{rand}` in every created code or URL so runs never collide, and `anon` for a request without the key.
- Error statuses come from the plan. In this app a code in the path that is malformed or unknown answers 404
  (never 422); 422 is only for a bad request body.
- Create every link a promise reads: start with `POST /api/links {url: '...{rand}'} -> 201 => link` and use
  `{link.code}`. To choose the code yourself, use `POST /api/aliases {url: '...', alias: 'x-{rand}'}`
  (`POST /api/links` ignores a `code` field). A `GET` of a code nobody created answers 404.
- Fail-first: every new behavior promise must fail on main, where the route doesn't exist yet (main answers 404
  to it). So a promise that expects 404 also passes on main: mark it `guard: true`. At least one promise per
  feature must not be a guard.
- Never write a flag-off (kill switch) promise; the checker can't switch flags. The Builder's tests cover it.
