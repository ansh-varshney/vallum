# Guardian notes for this repository

Extra instructions for Vallum's guardians in this codebase, on top of `skills/guardian-<type>/SKILL.md`.
Owner-written.

- Everything you need is in the Flight Director's task: the plan JSON with routes, files and done-when lines.
  Answer it in one go with a YAML list of promises; don't ask questions back.
- Routes in this app: links `POST /api/links`, `GET /api/links/{code}`; redirects `GET /r/{code}` (Core);
  pages return HTML (check them with `contains <text>`).
- Use `{rand}` in every created code or URL so runs never collide, and `anon` for a request without the key.
