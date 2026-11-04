# Run your own Vallum

This guide sets up the full hands-off loop in **your own GitLab project and Google Cloud project**: a member
moves an issue to *In progress*, agents build the change, and code merges, test-drives, releases and watches it.

It is the only way to see the loop run live. The original
[GitLab project](https://gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43503198/vallum) sits in a
hackathon group that was archived after the deadline: it is read-only, its schedule stopped on 27 Oct 2026 and
its flows can't run. Browse it for the record of every real run, and clone it as your starting point.

Everything here is taken from the code in this repository. Items marked **(check)** could not be confirmed
from the code alone; confirm them in your GitLab or Google Cloud settings.

> Only want to look around? You don't need any of this. See **Start here** in the [README](README.md):
> browse the archive of real runs on GitLab, or run the app, the control room and the 231 tests locally in 5 minutes.

## 1. What you need

| Need | Why | Notes |
|---|---|---|
| A **GitLab.com** project with **GitLab Duo Agent Platform** | The 6 agent flows (`.gitlab/duo/flows/`) run on it | Premium or Ultimate (a trial works), or Free with purchased GitLab Credits ([docs](https://docs.gitlab.com/ee/user/duo_agent_platform/)) |
| **Owner or Maintainer** role on that project | CI/CD variables, the approval rule, branch protection | Vallum was built with only Developer; see the table below |
| A **Google Cloud** project with billing on | Cloud Run (the app and the control room), Artifact Registry, Firestore | Idle cost is close to zero (section 9) |
| Locally: **Python 3.12**, `git`, the `gcloud` CLI | Tests, setup commands | |

GitLab features the code relies on that are not in every tier **(check)** for your plan:
merge request approval rules, scoped labels (`vallum::ready` and the other `vallum::` labels),
and work item statuses (Door A fires when the status changes to **In progress**).

### What Vallum did with only the Developer role, and what is easier with more

| Goal | With Developer only (how this project did it) | With Maintainer or Owner |
|---|---|---|
| Only reviewed code reaches `main` | The Merge Desk gives the owner's approval that the MR approval rule needs, and only after 12/12 rules pass | Same, plus protect `main` so nobody can push to it |
| Direct pushes never deploy | At that role `main` lets Developers (and so agent bots) push; the **provenance gate** stops such commits before any build or deploy and opens a revert MR (`vallum/gate/provenance.py`) | Protect `main` (push: no one); keep the gate as a second line |
| Protected paths stay the owner's | GitLab code-owner approval needs Maintainer, so **Gatekeeper rule 5** enforces the list in `CODEOWNERS` | Also turn on code-owner approval |

## 2. Create your GitLab project and clone the code

1. On GitLab.com create an **empty** project (no README). Don't push yet: the first push runs the main pipeline,
   which needs the Google Cloud setup (section 3) and the `OWNER_TOKEN` variable (section 5).
2. Note its **numeric project ID** (Settings → General) and its **path** (`group/subgroup/project`).
3. Clone this repository locally:

   ```bash
   git clone https://gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43503198/vallum.git my-vallum
   cd my-vallum
   ```

   A copy on GitHub works the same. Importing by URL in GitLab (New project → Import project → Repository by URL)
   also works, but then the first pipelines fail until sections 3–5 are done.

## 3. Google Cloud

The code calls these Google APIs: Cloud Run (`run.googleapis.com`, Knative v1 API, `vallum/drive/cloudrun.py`),
Firestore, Artifact Registry (Docker push in `build-image`), IAM Credentials and STS (keyless login,
`vallum/gcp.py` and `vallum/drive/gcp-token.sh`).

```bash
export PROJECT_ID=your-gcp-project            # your Google Cloud project ID
export REGION=us-central1                     # any Cloud Run region
export GITLAB_PROJECT_ID=12345678             # your GitLab project's numeric ID (section 2)
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud config set project "$PROJECT_ID"

# 1. APIs
gcloud services enable run.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

# 2. Artifact Registry: images are pushed to $REGION-docker.pkg.dev/$PROJECT_ID/vallum/app:<commit>
gcloud artifacts repositories create vallum --repository-format=docker --location="$REGION"

# 3. Firestore (default) database in Native mode: app data and Vallum's state store
gcloud firestore databases create --database='(default)' --location="$REGION" --type=firestore-native

# 4. Two service accounts
gcloud iam service-accounts create vallum-deployer --display-name="Vallum deployer (GitLab CI, keyless)"
gcloud iam service-accounts create vallum-app --display-name="Vallum app and control room (runtime)"
DEPLOYER="vallum-deployer@$PROJECT_ID.iam.gserviceaccount.com"
APP_SA="vallum-app@$PROJECT_ID.iam.gserviceaccount.com"

# deployer: create and replace Cloud Run services, make them public (allUsers invoker), push images,
# read and write the state store, and run revisions as vallum-app
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOYER" --role=roles/run.admin
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOYER" --role=roles/datastore.user
gcloud artifacts repositories add-iam-policy-binding vallum --location="$REGION" \
  --member="serviceAccount:$DEPLOYER" --role=roles/artifactregistry.writer
gcloud iam service-accounts add-iam-policy-binding "$APP_SA" \
  --member="serviceAccount:$DEPLOYER" --role=roles/iam.serviceAccountUser

# runtime: the app and the control room read and write Firestore
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$APP_SA" --role=roles/datastore.user

# 5. Workload Identity Federation: GitLab CI logs in with a short-lived OIDC token, no key files
gcloud iam workload-identity-pools create gitlab-pool --location=global --display-name="GitLab"
gcloud iam workload-identity-pools providers create-oidc gitlab-provider \
  --location=global --workload-identity-pool=gitlab-pool \
  --issuer-uri="https://gitlab.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.project_id=assertion.project_id,attribute.ref=assertion.ref,attribute.ref_protected=assertion.ref_protected" \
  --attribute-condition="assertion.project_id=='$GITLAB_PROJECT_ID' && assertion.ref=='main' && assertion.ref_protected=='true'"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/gitlab-pool/attribute.project_id/$GITLAB_PROJECT_ID"
```

| Check | Why |
|---|---|
| The provider accepts the audience in `.gitlab-ci.yml` (`.gcp: id_tokens: GCP_ID_TOKEN: aud`) | It is the provider's default audience, `https://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/gitlab-pool/providers/gitlab-provider` |
| `main` is a **protected** branch | The condition requires `ref_protected == 'true'`, and `OWNER_TOKEN` is a protected variable (new GitLab.com projects protect the default branch **(check)**) |
| Your organization allows public Cloud Run services | The test drive and the control-room job add `allUsers` as invoker (`CloudRun.ensure_public`) |
| No Cloud Run services exist yet | The first deploy creates `vallum` and `vallum-status` itself |

The exact attribute condition this project used isn't stored in the repo. The one above limits logins to your
project's protected `main`, which is what the jobs need **(check)**.

## 4. Point the code at your projects

These values are specific to the original project. Change them in **two commits**, in this order.
The provenance gate judges a push by `main`'s policy from **before** it, so the policy change must come first.

**Commit 1: who owns the rules, and a temporary setup switch** (`vallum/policy.yaml`, `CODEOWNERS`)

| File:line | Now | Change to |
|---|---|---|
| `vallum/policy.yaml:4` | `owners: [ansh-varshney]` | your GitLab username (the Merge Desk approves as the first owner) |
| `vallum/policy.yaml:5` | `project: { id: 87894346 ... }` | your GitLab project ID |
| `vallum/policy.yaml:104` | `cloud_run: { ..., region: us-central1, ..., project: vallum-512306 }` | your region and Google Cloud project ID (service names `vallum` and `vallum-status` can stay) |
| `vallum/policy.yaml` `provenance:` | `allow_owner_push: false` | `true` **for setup only** (section 7 turns it off) |
| `CODEOWNERS:3-12` | `@ansh-varshney` | your username |

```bash
git commit -am "Own the rules; allow owner pushes during setup"
```

**Commit 2: everything else**

| File:line | Now | Change to |
|---|---|---|
| `.gitlab-ci.yml:19-23` | `GCP_PROJECT_ID`, `GCP_PROJECT_NUMBER`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA` | your values (provider: `projects/<number>/locations/global/workloadIdentityPools/gitlab-pool/providers/gitlab-provider`; SA: `vallum-deployer@<project>.iam.gserviceaccount.com`) |
| `.gitlab-ci.yml:53` | `aud:` with project number `256944994567` | your project number |
| `.gitlab-ci.yml:203`, `:222` | environment URLs `https://vallum-256944994567.us-central1.run.app/`, `https://vallum-status-...` | `https://vallum-<number>.<region>.run.app/` and `https://vallum-status-<number>.<region>.run.app/` |
| `deploy/service.yaml:18` | `serviceAccountName: vallum-app@vallum-512306...` | `vallum-app@<your project>.iam.gserviceaccount.com` |
| `app/ui.py:7` | default `CONTROL_ROOM_URL` | your control-room URL (the test drive sets only `GOOGLE_CLOUD_PROJECT` on the app) |
| `promises/features/base-status.yaml:16`, `:21` | `/status` must redirect to the original control room | your control-room URL |
| `app/features/home.py:12` | `SOURCE` link to the original GitLab project | your project URL |
| `tests/test_gate.py:9,148,152,155,180,191` | `ansh-varshney` | your username (the tests load `vallum/policy.yaml`) |
| `tests/test_layer4.py:358,360` | original control-room URL | your control-room URL |

Defaults that CI overrides, so changing them is optional: `app/store.py:55`, `vallum/config.py:107,126`,
`vallum/gcp.py:16`, `vallum/gitlab.py:21`, `vallum/flows/__init__.py:174`, `vallum/flows/__main__.py:34`,
`control/main.py:14` (the control-room job sets `APP_URL`). Cosmetic: `vallum/schema/card.json:3`,
`control/__init__.py:2` and the README links. `docs/vallum/CODEMAP.md` is regenerated by the first housekeeping MR.

For a different app than this link shortener, also rewrite `vallum/project.yaml` (layout, test and start
commands) and `docs/vallum/GUIDELINES.md`; Vallum's own code holds no project specifics.

```bash
pytest                       # must stay green after the edits
git commit -am "Point Vallum at my GitLab and Google Cloud projects"
```

## 5. GitLab settings

### CI/CD variables (Settings → CI/CD → Variables)

| Variable | Protected | Masked | Holds |
|---|---|---|---|
| `OWNER_TOKEN` | ✅ | ✅ | A personal access token of the owner in `policy.yaml`, scope `api`. It reads and writes issues, MRs, approvals, merges, notes, Feature Flags and commits, publishes flows through GraphQL, and starts the Inspector through the Flows API (whether that also needs the `ai_workflows` scope: **(check)**). Note its **expiry date**: when it lapses, every main job fails. |
| `ALERT_WEBHOOK_URL` | ✅ | ✅ | Optional. A Slack- or Discord-style webhook; alerts are posted as JSON `{"content", "text"}` (`vallum/notify/__init__.py`) |

Everything else needs no secret:

| Name | Where it comes from |
|---|---|
| `GCP_PROJECT_ID`, `GCP_PROJECT_NUMBER`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA` | `variables:` in `.gitlab-ci.yml` (section 4) |
| `GCP_ID_TOKEN` | `id_tokens` in the `.gcp` template; only protected `main` jobs get a token Google accepts |
| `IMAGE`, `VALLUM_PROVENANCE`, `VALLUM_RELEASE_ID` | Passed between jobs as dotenv artifacts (`build-image`, `provenance-gate`) |
| `FIRESTORE_EMULATOR_HOST`, `GOOGLE_CLOUD_PROJECT` | Set by the `promise-checker` job (Firestore emulator; MR pipelines get no tokens and no cloud access) |
| `CHECKER_API_KEY` | Optional; the localhost checker defaults to `test-key` |
| The app's API key | Created by the test drive in Firestore (`vallum_secrets/app`) before the first deploy; never in the repo or CI |
| `CI_*`, `GITLAB_USER_LOGIN` | Provided by GitLab |

### Project settings

1. **GitLab Duo**: turn on Duo and the Agent Platform for the group or project, and allow flows from the
   AI Catalog **(check)** the exact settings for your plan.
2. **Merge request approval rule on `main`**: 1 approval required, eligible approver = the owner in
   `policy.yaml`. The Merge Desk gives this approval (`POST .../merge_requests/:iid/approve` with the exact SHA)
   only after all 12 Gatekeeper rules pass. If you open MRs yourself (Door B), make sure the author may
   approve **(check)**; the desk says so when an approval is refused.
3. **GitLab Duo Code Review**: turn on automatic reviews **(check)**. The review instructions in
   `.gitlab/duo/mr-review-instructions.yaml` are generated from `vallum/RULEBOOK.md`.
4. **Label `vallum`**: create it once. The Feature template applies it with `/label ~vallum`, and quick actions
   only apply labels that exist. The other labels in `policy.yaml` (`vallum::ready`, `vallum::ledger`, ...)
   are added through the API when first used **(check)** that they are created automatically on your instance.
5. **Container Registry** on (the `build-image` job pushes a copy for GitLab container scanning).
6. **Issue template**: `.gitlab/issue_templates/Feature.md` is picked up automatically.

## 6. First push

```bash
git remote add mine https://gitlab.com/<your-group>/<your-project>.git
git push mine main
```

The two commits from section 4 go up together. The head commit is judged by the policy in commit 1
(your username, owner pushes allowed), so the provenance gate lets it through as `owner-push`, recorded as an override.

| Job (main pipeline) | What it does on the first run |
|---|---|
| `provenance-gate` | Allows the owner push and records it |
| `flows-sync` | Publishes the 6 flows to the AI Catalog, enables them in your project (each gets its own bot account) and creates their triggers. It runs on main pushes that change `.gitlab/duo/flows/` or `vallum/flows/`; otherwise run it by hand from a web or scheduled pipeline |
| `unit-tests`, `semgrep-sast`, `secret_detection` | Tests and scans |
| `build-image`, `container_scanning` | Builds the image into Artifact Registry, scans it |
| `test-drive` | Creates the `vallum` service (the first release has no old revision), generates the app API key, runs step 0 on the private address, then moves traffic by the tier's steps |
| `control-room` | Creates the `vallum-status` service with all traffic |
| `merge-desk` | Looks for ready MRs (none yet) |

The flow triggers `flows-sync` creates (`vallum/flows/__init__.py`):

| Flow | Trigger | Started by |
|---|---|---|
| Ship | work item status changed to **In progress** (Door A); bot added as reviewer (Door B, and code asking for a review or fix) | A member, or code acting as the owner |
| Inspector | none active; started through the Flows API with an evidence bundle | The Merge Desk |
| Pipeline Medic | bot added as reviewer | The Merge Desk, when an MR pipeline fails |
| Release Captain | bot assigned | The test drive, after a release |
| Incident Commander | bot assigned | The test drive or Live Patrol, after a rollback |
| Cockpit | bot mentioned | Anyone; code carries out commands for members only |

## 7. Schedule and lock down

1. **Pipeline schedule** (Build → Pipeline schedules → New): target `main`, interval `*/10 * * * *`
   (every 10 minutes, as this project ran). It runs **Live Patrol** (live promise checks, allowances and modes,
   security triage, kill switches, the Watchdog, `@vallum` commands) and the **Merge Desk**. Scheduled pipelines
   run as the schedule's owner, so create it as the owner.
2. **Turn owner pushes off again**: set `provenance: allow_owner_push: false` in `vallum/policy.yaml`, commit and
   push. That push is judged by the previous policy (`true`), so it passes the gate; every later push to `main`
   that skipped the Merge Desk is stopped before build or deploy, and a revert MR opens.
3. Optional, with Maintainer: protect `main` so nobody can push to it, and turn on code-owner approval.

## 8. First run: Door A

1. **Issues → New issue**, template **Feature**. Write what it should do, who it is for, how you'll know it
   works (one line each) and how important it is (Core, Standard or Experimental).
2. Move the issue's status to **In progress**. That is the last human step.
3. Watch it (in this project it took about 15 minutes from *In progress* to 100% of visitors):

| Where | What you should see |
|---|---|
| The issue | The Flight Director's kickoff note, then the Planner and guardians' work |
| A new MR `vallum/<issue>-<feature>` | Code, tests and a Feature Card, opened by the Builder; Reviewer and GitLab Duo reviews; the MR pipeline with the promise checker and fail-first |
| The MR, a few minutes later | The Merge Desk: Inspector "matches", 12/12 rules, approval and merge |
| The main pipeline | Provenance gate (`merge-desk`), build, scan, test drive steps, release |
| The issue again | Release notes from the Release Captain; the issue is closed |
| A housekeeping MR | Refreshes `docs/vallum/CODEMAP.md` |
| The **Ledger** issue (`🛡 Vallum Ledger`, label `vallum::ledger`) | Created by code on first use and rewritten after every change |
| The control room | `https://vallum-status-<project number>.<region>.run.app/`: overview, releases (live lane), features, allowance, security, rules |

Door B: open your own MR with a Feature Card and add the Ship flow's bot as reviewer.
Emergency: run the manual job `rollback-now` in any main pipeline, or comment `@vallum rollback <feature>`.

## 9. Costs

| Item | What it costs |
|---|---|
| Cloud Run | Both services scale to zero (`minScale: "0"`, `deploy/service.yaml`); you pay for requests and cold starts |
| Firestore | A few small documents and the app's links; usually within the free tier |
| Artifact Registry | **Every release pushes a new image and nothing deletes old ones.** Add a cleanup policy that keeps the last few images **(check)** sizes in the console |
| GitLab CI | Live Patrol every 10 minutes is 144 pipelines a day, and after a `@vallum` command a patrol job listens for up to 25 minutes (`cockpit.job_listen_max_min`). On a plan with limited compute minutes, schedule it less often |
| GitLab Duo credits | Measured on this project: about **55 credits** for an incident loop, its fix and one feature; 97% of credits went to agent flows. The credit guard turns on Safe mode after **40** code-started agent sessions in a day (`credits.daily_code_started_sessions`) |

## 10. Safety notes

- Never commit tokens. `OWNER_TOKEN` lives only in a protected, masked variable, so only protected `main`
  pipelines see it. MR pipelines run with no token and no cloud access.
- Workload Identity Federation means **no key files**: each job exchanges its GitLab OIDC token for a
  short-lived Google token, and the provider only accepts your project's protected `main`.
- The app's API key is generated into Firestore (`vallum_secrets/app`); read it in the console when you need it.
- Rotate `OWNER_TOKEN` before it expires.

## 11. Troubleshooting (real failures from building Vallum)

| Symptom | Cause | Fix |
|---|---|---|
| Ship's sub-agents ignore their task | The supervisor's delegation arrives as `{{ goal }}`; a sub-agent without the `context:goal` input never sees it | Every sub-agent in `ship.yaml` takes `context:goal` and prints `{{ goal }}` in its prompt |
| `flows-sync` is rejected by GitLab.com | The flow validator forbids `max_cycles`, response schemas, top-level `name`/`description` and HTML comments in prompts | Remove them from the flow YAML |
| Firestore: "Property ... contains an invalid nested entity" | Firestore can't store an array inside an array | Flatten it, e.g. join IDs into one string (`vallum/watch.py`) |
| A healthy release is rolled back under load (r-0032) | The visitor simulator overloaded one CI runner (at 133 req/s every route took 3–4 s) | Keep `simulate.max_rps` at 100 or below |
| A new revision's first seconds look slow | Cold start and the traffic switch | `judge.warmup_s: 10` skips them |
| `build-image` fails: `control/` missing | `.dockerignore` excluded it | The image must contain `app/` and `control/` |
| Your push to `main` is stopped and a revert MR opens | The provenance gate: only Merge Desk merges deploy | Expected. During setup use `allow_owner_push` (sections 4 and 7) |
| The Merge Desk can't approve | The approval rule doesn't list the owner, or author approval is blocked | Fix the rule (section 5) |
| An agent mixes up issue #16 and MR !16, or picks the wrong status code | Ambiguous guidance | Write the convention down in `docs/vallum/GUIDELINES.md` or `docs/vallum/roles/<role>.md`; every agent reads them |
| Nothing happens when the issue moves to *In progress* | Flows not published, the trigger is missing, or the status name differs | Run `flows-sync`; check the flow's triggers in your project; the status must be named exactly **In progress** |
| A main job says `GCP_ID_TOKEN is missing` or the STS exchange fails | The job didn't run on protected `main`, or the provider condition doesn't match | Protect `main`; compare the condition with your project ID (section 3) |
