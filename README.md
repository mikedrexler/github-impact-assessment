# GitHub Impact Assessment

A local dashboard that polls **GitHub Status**, discovers repositories visible to your existing credentials, and correlates unresolved incidents with **read-only evidence** in those repositories. It never dispatches workflows, creates canaries, or changes a monitored repository.

**Unknown is not unaffected.** A public incident is not proof of your impact, and a successful metadata read is not proof that a service works. This application deliberately cannot promise definitive impact detection for every GitHub service.

## Run

Requires Python **3.11+**. The runtime and test suite use only the standard library; no dependency installation is needed.

```sh
git clone https://github.com/mikedrexler/github-impact-assessment.git
cd github-impact-assessment
# Uses GH_TOKEN, then GITHUB_TOKEN, then existing gh authentication for github.com.
python3 -m impact
```

Open **http://127.0.0.1:8765**. The server binds to loopback only. Leave the process running for monitoring; Ctrl-C or SIGTERM stops it. It is not a hosted service, launch agent, or background daemon by default. No monitoring occurs after exit or while the computer sleeps. On some systems use `python` rather than `python3`.

To try synthetic incident paths without credentials or network requests:

```sh
python3 -m impact --demo
# Or use a different port if the live monitor is already running:
IMPACT_PORT=8766 python3 -m impact --demo
```

The demo is prominently labeled **FIXTURE DEMO**, includes a queued-job symptom, a self-hosted runner confounder, a permission failure and Pages/Webhooks limitations. Its times are relative to startup; its responses are deterministic otherwise. It is not a live health assessment.

Optional installation: `python3 -m pip install .` exposes the `github-impact-assessment` command. The source-tree command above needs no pip/setuptools.

## Authentication, scope and privacy

- Reuse existing `gh` authentication or an already available environment token. The app does not request, create, broaden, or refresh tokens/organization permissions. Tokens stay server-side, in memory, and are attached **only** to GET requests to `https://api.github.com`. Redirects are rejected. The public Status API receives no credentials.
- Repository enumeration paginates `GET /user/repos` with owner, collaborator and organization-member affiliations. This is the meaningful enumerable "all accessible repositories" scope, **not all public repositories on GitHub**, and not a guarantee of all repositories your user account can access. Fine-grained token selection, GitHub App identity, organization policy, SSO and permissions can silently hide repositories. Every dashboard snapshot carries this caveat even when pagination is exhausted. This version targets **github.com**, not GHES or `.ghe.com`.
- Existing credentials must have access to the relevant repositories. Depending on token type, the probes need read access to metadata, Contents (commit sample), Actions runs/jobs, Checks, Issues, Pull requests and Pages. Missing access produces an explicit unknown/error; do not grant admin or write permissions just to improve this dashboard. Classic PATs/OAuth tokens may have broader scopes than needed; the application still sends GET only.
- An SSO `partial-results` response marks enumeration partial. A 403 is labeled permission-or-secondary-limit (GitHub does not always distinguish these via headers), and a 404 can mean absent **or hidden**. SSO can also silently hide entire organizations. Authentication failures, service errors and rate limits never count as confirmed impact.
- No request logs, workflow logs, webhook payloads, private code, repository secrets or raw error response bodies are collected. Evidence is reduced to names, timestamps, status/conclusions, counts and links. Repository names/evidence remain private data: do not expose this port, reverse proxy it, or share its API responses publicly. Any local user/process with access to loopback can read the dashboard; it is not an authenticated multi-user server.
- No browser token, CORS, external scripts/fonts, telemetry, browser storage, or persistent assessment file. Strict Host/Origin checks mitigate cross-site refresh and DNS rebinding; a CSP blocks remote assets and framing. Browsing evidence links is user-initiated with no referrer. History contains at most 10 summary snapshots **in server memory**, cleared on restart.
- The app honors the Python runtime's proxy/CA configuration. Use only a trusted local machine, proxy and Python/`gh` installation. `.env` and generated Python/package output are ignored by git. `.env.example` documents variables; the app **does not automatically load `.env` files**. Never put tokens on command lines, in source, in URLs, or in this README.

## Monitoring and bounded coverage

The first assessment starts immediately; status + inventory are refreshed each cycle, including when there are no incidents. Repository probes run **only** for unresolved incidents with matching live component IDs. No incident is fabricated. A degraded component without a mapped incident is explicitly flagged. Unscoped incidents and newly introduced components are surfaced as gaps instead of guessed from titles.

The dashboard offers manual refresh, incident/component cards, per-repository evidence and error reasons, inventory, filtering, sample limits, timestamps, freshness, and in-memory history. It polls the local server every 5 seconds; that does not cause a GitHub poll. Only one assessment runs at a time; overlapping manual requests deduplicate, with a 10-second manual-refresh throttle. Periodic polling is measured **after the previous assessment completes**. Exceptions retain the previous timestamped snapshot with a visible failure/staleness warning.

Environment settings (strictly validated integers):

| Variable | Default | Bounds / meaning |
|---|---:|---|
| `IMPACT_POLL_SECONDS` | 300 | 60..86400; delay after each completed cycle |
| `IMPACT_TIMEOUT_SECONDS` | 10 | 1..60; socket timeout for each HTTP attempt |
| `IMPACT_CYCLE_SECONDS` | 120 | 10..1800; cycle deadline, stops starting requests |
| `IMPACT_MAX_REQUESTS` | 500 | 1..10000; all Status/GitHub attempts, including retries, per cycle |
| `IMPACT_MAX_REPOSITORIES` | 10000 | 1..100000; inventory cap, reported as partial when exceeded |
| `IMPACT_MAX_CHECK_REPOSITORIES` | 200 | 1..10000; rotating incident-time repository batch |
| `IMPACT_MAX_ASSESSMENTS` | 2000 | 1..20000; total repository/incident/component rows per cycle |
| `IMPACT_WORKERS` | 4 | 1..16; bounded simultaneous repository probes |
| `IMPACT_PORT` | 8765 | 1024..65535; loopback dashboard port |

Example: `IMPACT_MAX_CHECK_REPOSITORIES=50 IMPACT_POLL_SECONDS=600 python3 -m impact`.

Enumeration uses pages of 100 until exhausted, denied, timed out, or budget-limited. Its phase receives at most half the cycle time and half the request budget (when the total budget is at least 4) so large inventories do not consume all resources before incident probes begin. An in-flight request/retry can overrun the phase, but not start beyond the total cycle budget. Responses are capped at 8 MiB each. Transient network errors and HTTP 5xx retry at most twice (1s/2s cancellable waits), within the request/time budgets. HTTP 401/404/other 4xx are not retried. Rate-limit responses set a shared cooldown (at least 60 seconds, honoring numeric Retry-After and quota reset); 403 without definitive headers also conservatively sets a cooldown because it could be a secondary limit. Manual refresh cannot bypass it. Status requests remain independent of GitHub API cooldown.

The deadline prevents new network attempts, not instantaneous interruption of in-flight socket I/O. Shutdown cancels pending starts/backoff and waits briefly for in-flight reads. Offline, permission, API errors and budget exhaustion remain visible as unknown/partial; they never become an all-clear.

Repositories outside a batch rotate into later cycles. Inventory itself restarts each cycle: if your visible inventory exceeds its cap/deadline, increase those limits cautiously or accept that enumeration beyond the cap is not covered. Credential changes or inventory changes can change rotation order. Assessment-row caps can also omit component pairs within a batch; all omitted counts are shown. **A partial cycle and historical samples are not a full-environment audit.** "Bounded cycle complete" means the credential-visible enumeration and scheduled probes finished; it does not mean every service was tested, full account access was verified, or every Actions run/job was scanned.

## Evidence adapters and interpretation

Current component names were inspected directly from the live [Status JSON API](https://www.githubstatus.com/api/v2/summary.json). The app discovers IDs/names on every poll; names select adapters and IDs correlate incidents. New/unrecognized names use an explicit unsupported adapter. The live feed currently includes the following components:

| Status component | Actual read-only evidence | What it cannot establish |
|---|---|---|
| Actions | 30 most recent runs; first 100 jobs and first 100 commit check runs for up to 5 incident-overlapping runs per repository/incident | Exhaustive history, execution, dispatch, runner connectivity/capacity, log diagnosis, or absence of impact |
| Git Operations | One commit reference via REST | Git clone/fetch/push transport or write availability |
| API Requests | Repository metadata GET | Health of other endpoints, GraphQL, write operations or sustained availability |
| Pull Requests | One open PR via REST | Creating/merging PRs, reviews, notifications or UI health |
| Issues | One issue via REST (endpoint may include PRs) | Creation/editing, notifications or UI health |
| Pages | Pages configuration, only where inventory reports a configured site | Builds, deployment, DNS or published-site availability |
| Webhooks | Explicit unknown; no delivery/configuration request | Payload delivery requires privileged/sensitive evidence; never replayed |
| Packages | Explicit unknown | Package download/upload health; no universal repository-level probe |
| Codespaces | Explicit unknown | Provisioning, compute, connection or lifecycle health; no canary |
| Copilot | Explicit unknown | IDE inference, chat, agent or tenant service availability |
| Copilot AI Model Providers | Explicit unknown | Provider/model inference availability |
| Visit www.githubstatus.com for more information | Explicit unsupported/informational | No tenant-level service probe |

Classifications:

| Classification | Meaning |
|---|---|
| **confirmed** | Reserved for direct attributable tenant impact. **No current adapter emits this**; available evidence cannot responsibly prove GitHub causation. |
| **suspected** | A currently queued/waiting job created at least 10 minutes ago during an unresolved Actions incident, without an explicit self-hosted label. This is a correlated symptom, **not confirmation**; runner type/capacity, concurrency controls and approvals remain confounders. |
| **unknown** | Insufficient or unsupported evidence; successful metadata reads; missing access; rate limits, timeouts, service failures, historical/application failures or unproven queue timing. |
| **not-applicable** | Inventory explicitly reports Issues disabled or no configured Pages site. This is about that feature on that repository, not a component health statement. |

Actions timing uses incident `started_at` (falling back to announcement `created_at`), not titles or arbitrary failure counts. Completed runs last updated before that point are historical; active runs can overlap. Incident onset can precede announcement, so samples can miss real impact. A job's **creation age is not continuous queue duration**. Self-hosted queues remain unknown rather than being attributed to GitHub. Application test failures, timeouts and external check-app failures are recorded without assuming platform failure. Even successful incident-time runs do not establish "unaffected." Missing job creation time prevents a queue-age inference.

## Architecture

```text
Browser (local static HTML/CSS/JS; no credentials)
  -> loopback Python HTTP server: GET /api/state, POST /api/refresh
     -> single-flight scheduler + 10-entry in-memory summary history
        -> public GitHub Status summary GET (no auth)
        -> credential-visible /user/repos pagination
        -> bounded concurrent GET-only evidence adapters
```

- `impact/client.py`: environment/`gh` credential resolution, fixed-origin requests, retry/backoff, pagination and budgets.
- `impact/assessment.py`: live component mapping, conservative classifications and evidence projection.
- `impact/monitor.py`: polling, cancellation, overlap control, rotating batches and snapshots.
- `impact/app.py` and `impact/static/`: local server and responsive dashboard.
- `impact/demo.py`: network-free incident fixtures.

**No agents, MCP servers, AI models, external databases, hosted services or GitHub Actions workflows are required or created for this application.** All assessment logic is deterministic Python. The only application outbound destinations are GitHub Status and GitHub's REST API (subject to your trusted proxy settings). There are no active checks or monitored-repository mutations. A dashboard refresh is a local scheduling operation, not a GitHub write.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q impact tests
# Optional development environment for static type checking:
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m mypy impact
# Alternative type checker (requires Node.js/npm):
npm exec --yes --package=pyright -- pyright impact
# Optional JS parse check if Node.js is installed:
node --check impact/static/app.js
# Build the installable wheel (requires pip and the declared setuptools build backend):
python3 -m pip wheel --no-deps --wheel-dir dist .
```

The offline unittest suite covers all current/future component mapping, no-incident/unknown status paths, pagination, SSO/permission/rate-limit errors, retries/budgets, incident timing, queue thresholds, self-hosting, application failure non-attribution, scheduler deduplication/periodic polling, bounded history, HTTP routes, cross-origin rejection and credential boundaries. Mypy checks the Python application (including annotated and unannotated bodies); runtime guards validate external response shapes. The frontend is served directly without a JavaScript build pipeline.

## Troubleshooting

- **Authentication unknown/401:** confirm your existing `gh auth status --hostname github.com` or environment credential locally; the app re-resolves credentials each cycle. Do not paste token values into issues/logs.
- **403/404 or missing organization:** check existing SSO authorization/token repository selection and organization policy. The app will not acquire new permissions. A cooldown after 403 is intentional.
- **Partial coverage:** inspect inventory reason, deferred counts, probe errors and sample limits before making decisions. Increasing a cap does not bypass permission or GitHub rate limits.
- **Status unavailable:** inventory may still be visible, but incident state is unknown; no tenant health conclusion follows. No stale feed is relabeled current.
- **Unexpected assessment failure:** the prior snapshot is retained with a visible error kind, not raw server data. Restart, try `--demo`, and run the offline suite; investigate response-shape or runtime changes without exposing private evidence.
- **Address in use:** choose another `IMPACT_PORT`. Do not change the loopback binding to publish the dashboard.
