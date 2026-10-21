# The feature catalog

The kinds of change Vallum accepts. Each kind sets the promise types a Feature Card must have and a default tier.
Anything not listed goes to a human. Machine-readable version: [`catalog.yaml`](catalog.yaml).

| Kind | Example | Required promise types | Default tier |
|---|---|---|---|
| `new-endpoint` | Bulk-create links | behavior, speed, reliability, security | Standard |
| `behavior-change` | Links expire after 30 days | behavior, compatibility, data | The feature's tier |
| `ui-page` | Dashboard of top links | behavior, speed (page load), security | Standard |
| `background-job` | Nightly cleanup of expired links | behavior, data, cost | Standard |
| `integration` | Slack alert at 1,000 clicks | behavior, reliability, security | Experimental |
| `data-change` | Store click country | data, compatibility, behavior | Core if it touches redirects |
| `performance` | Cache redirects | speed, behavior, cost | The feature's tier |
| `config-infra` | Keep one instance warm | speed, cost, operability | App-wide |
| `dependency` | Upgrade FastAPI | compatibility, security | App-wide |
| `bug-fix` | Aliases with capital letters fail | behavior, plus a learned promise | The feature's tier |
| `security-fix` | Block open-redirect abuse | security, behavior | The feature's tier |

**Never allowed** (labelled `vallum::needs-human` with an explanation): payments, rewriting login or auth,
deleting user data, legal or policy text, handling secrets, and anything [`RULEBOOK.md`](RULEBOOK.md) excludes.
