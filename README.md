# GitHub Impact Assessment

A local dashboard that polls **GitHub Status**, assesses repositories you preselect (or explicitly opt in to discover), and correlates unresolved incidents with **read-only evidence** in those repositories. It never dispatches workflows or changes a monitored repository. A separate, explicitly enabled **active Copilot inference check** can provide shared client/model-path evidence; it is potentially billable and is not a read-only API probe.

**Unknown is not unaffected.** A public incident is not proof of your impact, and a successful metadata read is not proof that a service works. This application deliberately cannot promise definitive impact detection for every GitHub service.

## Run

Requires Python **3.11+**. The default runtime and test suite use only the standard library; no dependency installation is needed. The optional active Copilot check requires the separately installed SDK described below.

```sh
git clone https://github.com/mikedrexler/github-impact-assessment.git
cd github-impact-assessment
# Uses GH_TOKEN, then GITHUB_TOKEN, then existing gh authentication for github.com.
python3 run.py
```

You can also launch **from any directory** using the checkout's file path; Python automatically resolves the bundled `impact` package beside the launcher:

```sh
python3 /absolute/path/github-impact-assessment/run.py
```

Keep `run.py` next to the `impact/` directory; do not copy it out of the checkout. `python3 -m impact` remains supported **from the repository root**, or from any directory after installing the package into that same Python interpreter. Without one of those conditions, Python correctly reports `No module named impact`. No global installation or `PYTHONPATH` modification is needed for the file launcher.

Open **http://127.0.0.1:8765**. The server binds to loopback only. Leave the process running for monitoring; Ctrl-C or SIGTERM stops it. It is not a hosted service, launch agent, or background daemon by default. No monitoring occurs after exit or while the computer sleeps. On some systems use `python` rather than `python3`.

### Preselect repositories instead of crawling organizations

Normal startup **does not enumerate repositories**. Public Status polling starts, but repository checks remain paused until you open the **Configuration** tab and choose a scope in **Repositories to assess**. Navigation is **Live Assessment / Past Incidents / Configuration** on both pages. Switching tabs preserves unapplied repository edits and does not trigger discovery or assessment. The Configuration tab can be linked directly using `/#configuration`, and browser back/forward navigation is supported.

1. In **Repositories to assess**, the compact **Discover and add repositories** group combines **Discover all accessible repositories** with the **Add repository to assess** dropdown. The dropdown is greyed out until you explicitly click discovery (or launch with `--all-repositories`). It remains disabled if discovery has not yielded any available names. Discovery opts into account-wide enumeration and assessment; apply a selected list afterward to restrict ongoing checks. Alternatively, use the unchanged manual **Selected repositories** field below to enter exact `owner/repo` names, one per line or comma-separated, and avoid enumeration entirely. Dropdown choices add to a draft list; remove a name from that list to exclude it. Choosing a dropdown item does not itself trigger discovery.
2. Use **Select all available repositories** to add all names currently listed in the dropdown to your draft at once, retaining manually entered names and deduplicating case-insensitively. It follows the dropdown's discovery opt-in and disabled states. This selects only the current bounded inventory, not undiscovered repositories; partial discovery remains partial, and later discoveries are not automatically added. If the combined draft exceeds the displayed selection limit, the action reports the limit and leaves your draft unchanged rather than truncating it. Click **Assess selected repositories** to apply your draft. The server validates only those names via `GET /repos/{owner}/{repo}` and assesses only successfully validated selected repositories when mapped incidents exist. No `/user/repos` or organization-wide enumeration occurs when applying explicit selection.
3. Use **Pause repository checks** to stop repository discovery/probes while retaining public Status polling. Discovery is optional for manually entered names. The explicit discovery opt-in is remembered for that server process, including after switching to selected/paused scope, but resets on restart. Discovery can be partial or fail because of permissions; enabling the dropdown is not a guarantee of complete account coverage.

Names are deduplicated case-insensitively. URLs, paths, query strings, invalid names and selections larger than `min(IMPACT_MAX_REPOSITORIES, 200)` are rejected. The selection is **server-memory-only**, shared by tabs using that server, and clears on restart; it is not saved in browser storage or a config file. To start immediately with the same explicit list on each launch, use:

```sh
python3 run.py --repos my-org/service-api my-org/web-app
# Explicit opt-in to the previous account-wide discovery behavior:
python3 run.py --all-repositories
```

`--repos` and `--all-repositories` are mutually exclusive. Changing scope queues a switch after the current bounded assessment/discovery batch finishes; the UI labels this pending state. The switch clears old inventory, evidence, summary history and rotation before scheduling the new scope. It does not interrupt an in-flight HTTP request or bypass existing time/request/backoff limits.

Selected metadata validation uses the existing paced discovery budgets and resumes at the next unfinished selected name. Missing permissions, 404/hidden or renamed repositories, invalid responses and SSO restrictions remain visible coverage gaps; they are not silently replaced or considered unaffected. Only exact matching identities are accepted. Failed names are retried on the normal discovery interval, while rate/time/request checkpoints retain their existing cooldown/resume behavior. A selected repository's metadata can be revalidated without scanning any organization's repository list.

To try synthetic incident paths without credentials or network requests:

```sh
python3 run.py --demo
# Or use a different port if the live monitor is already running:
IMPACT_PORT=8766 python3 run.py --demo
# Exercise explicit selection against the fixtures:
python3 run.py --demo --repos example/queue-symptom example/self-hosted
```

The demo is prominently labeled **FIXTURE DEMO**, includes a queued-job symptom, a self-hosted runner confounder, a permission failure and Pages/Webhooks limitations. Demo startup without scope flags loads all three fixture repositories, not your actual accounts; the selection interface also works with those fixture names. Its times are relative to startup; its responses are deterministic otherwise. It is not a live health assessment.

Optional installation for launching by module or command from any directory (use a dedicated environment, not the system Python):

```sh
# Run these setup commands from the repository root:
python3 -m venv .venv
.venv/bin/python -m pip install .
# From any directory, use the installed environment's absolute path:
/absolute/path/github-impact-assessment/.venv/bin/github-impact-assessment
# Equivalently:
/absolute/path/github-impact-assessment/.venv/bin/python -m impact
```

Installing into `.venv` does not install into a different `python3` on your PATH. The source-tree `run.py` command needs no pip/setuptools and is the recommended dependency-free startup path.

## Authentication, scope and privacy

- Reuse existing `gh` authentication or an already available environment token. The app does not request, create, broaden, or refresh tokens/organization permissions. Repository-monitoring tokens stay server-side, in memory, and are attached **only** to GET requests to `https://api.github.com`. Redirects are rejected. The public Status API receives no credentials. The optional planner and active Copilot check use separate server-side credentials, described below.
- Explicit selection reads only exact selected repository metadata and incident evidence; it does not enumerate accounts or organizations. Optional account-wide discovery paginates `GET /user/repos` with owner, collaborator and organization-member affiliations. This is the meaningful enumerable "all accessible repositories" scope, **not all public repositories on GitHub**, and not a guarantee of all repositories your user account can access. Fine-grained token selection, GitHub App identity, organization policy, SSO and permissions can silently hide repositories. Every dashboard snapshot carries its scope/access caveat. This version targets **github.com**, not GHES or `.ghe.com`.
- Existing credentials must have access to the relevant repositories. Depending on token type, the probes need read access to metadata, Contents (commit sample), Actions runs/jobs, Checks, Issues, Pull requests and Pages. Missing access produces an explicit unknown/error; do not grant admin or write permissions just to improve this dashboard. Classic PATs/OAuth tokens may have broader scopes than needed; the application still sends GET only.
- An SSO `partial-results` response marks enumeration partial. A 403 is labeled permission-or-secondary-limit (GitHub does not always distinguish these via headers), and a 404 can mean absent **or hidden**. SSO can also silently hide entire organizations. Authentication failures, service errors and rate limits never count as confirmed impact.
- No request logs, workflow logs, webhook payloads, private code, repository secrets or raw error response bodies are collected. Evidence is reduced to names, timestamps, status/conclusions, counts and links. Repository names/evidence remain private data: do not expose this port, reverse proxy it, or share its API responses publicly. Any local user/process with access to loopback can read the dashboard; it is not an authenticated multi-user server.
- No browser token, CORS, external scripts/fonts, application telemetry, browser storage, or persistent assessment file. Strict Host/Origin checks mitigate cross-site refresh and DNS rebinding; a CSP blocks remote assets and framing. Browsing evidence links is user-initiated with no referrer. History contains at most 10 summary snapshots **in server memory**, cleared on restart. The opt-in active check persists only its attempt timestamps for the hourly cap, not credentials or evidence.
- The app honors the Python runtime's proxy/CA configuration. Use only a trusted local machine, proxy and Python/`gh` installation. `.env` and generated Python/package output are ignored by git. `.env.example` documents variables; the app **does not automatically load `.env` files**. Never put tokens on command lines, in source, in URLs, or in this README.

## Monitoring and bounded coverage

Startup polls Status with repository work paused unless a startup scope is configured (demo defaults to fixture scope). With a scope enabled, startup performs one short selected-validation or account-discovery batch, then the first assessment. Later assessments read the latest scoped inventory snapshot without waiting for enumeration. Repository discovery/validation has its own paced background schedule, including when there are no incidents. Repository probes run **only** for unresolved incidents with matching live component IDs. No incident is fabricated. A degraded component without a mapped incident is explicitly flagged. Unscoped incidents and newly introduced components are surfaced as gaps instead of guessed from titles.

The dashboard offers manual refresh, incident/component cards, per-repository evidence and error reasons, inventory, filtering, sample limits, timestamps, freshness, and in-memory history. It polls the local server every 5 seconds; that does not cause a GitHub poll. Only one worker performs network work: due assessments take priority over discovery batches, so they never overlap or multiply request concurrency. A running discovery batch may delay an assessment until the batch yields. Manual assessment requests deduplicate, with a 10-second throttle. Periodic assessment polling is measured **after the previous assessment completes**. Discovery-only batches do not rerun incident probes, advance check rotation, or add history entries. Exceptions retain the previous timestamped snapshot with a visible failure/staleness warning.

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
| `IMPACT_DISCOVERY_SECONDS` | 15 | 1..60; time budget per discovery-only batch |
| `IMPACT_DISCOVERY_REQUESTS` | 10 | 1..100; attempts per discovery-only batch, also capped by `IMPACT_MAX_REQUESTS` |
| `IMPACT_DISCOVERY_RESUME_SECONDS` | 5 | 1..300; pause after a discovery time/request checkpoint before the next batch |
| `IMPACT_WORKERS` | 4 | 1..16; bounded simultaneous repository probes |
| `IMPACT_PORT` | 8765 | 1024..65535; loopback dashboard port |

Example: `IMPACT_MAX_CHECK_REPOSITORIES=50 IMPACT_POLL_SECONDS=600 python3 -m impact`.

Enumeration uses pages of 100. By default, each background batch starts at most 10 HTTP attempts and runs for at most 15 seconds before checkpointing; a checkpoint resumes after 5 seconds, not after the 300-second assessment poll. Every discovery batch, including startup, has its own bounded client; its attempts are **additional to assessment-cycle attempts**, not a global hourly allowance. Set the three `IMPACT_DISCOVERY_*` settings deliberately if you need slower discovery. Assessment clients retain `IMPACT_MAX_REQUESTS` and `IMPACT_CYCLE_SECONDS`; the standalone `assess()` fallback without a supplied inventory snapshot retains its half-cycle inventory budget.

An in-flight read can overrun a phase's wall-clock budget, but clients do not start attempts past their deadline. Responses are capped at 8 MiB each. Transient network errors and HTTP 5xx retry at most twice (1s/2s cancellable waits), within each client's request/time budgets. HTTP 401/404/other 4xx are not retried. Discovery and assessment share one GitHub API cooldown: rate-limit responses set at least a 60-second pause, honoring numeric Retry-After and quota reset. A 403 without definitive headers also conservatively sets a cooldown because it could be a secondary limit. Discovery retries service/auth/permission/network/SSO/capacity errors no faster than the normal poll interval, and rate-limit retries wait at least until the shared cooldown expires. Neither manual refresh nor checkpoint continuation bypasses it. Status requests remain independent of GitHub API cooldown.

The deadline prevents new network attempts, not instantaneous interruption of in-flight socket I/O. Shutdown cancels pending starts/backoff and waits briefly for in-flight reads. Offline, permission, API errors and budget exhaustion remain visible as unknown/partial; they never become an all-clear.

### Filtering repository evidence

The **Repository** dropdown in **Repository evidence** lists repositories with results in the current assessment. Select one repository or **All repositories**, and optionally combine it with the text filter. The selection survives dashboard refreshes while that repository still has results; it resets to all repositories if the repository leaves the assessment. This is a local display filter only: it does not change monitoring scope or trigger repository discovery/probes. Use **Repositories to assess** in the **Configuration** tab to change the assessed scope.

### Workaround for large inventories: resumable discovery

This section describes opt-in **account-wide** discovery. Prefer explicit selection above if only a few repositories need monitoring.

Discovery **continues at the next unfinished page** after time/request limits, transient failures, or rate limits rather than restarting at page one. Each background batch adds to a bounded, memory-only inventory. **`inventory_deadline` is now a resumable checkpoint, not a five-minute stall:** the scheduler queues another discovery-only batch after `IMPACT_DISCOVERY_RESUME_SECONDS`. The coverage panel labels expected time/request checkpoints as **Checkpoint saved**, while genuine errors remain visible. Manual refresh reads the cache instead of restarting enumeration and respects the same backoff.

The coverage panel shows live background inventory progress separately from the last assessment's inventory/counts. It includes the next batch time, next page, current-pass count/start time, last unrestricted completion time, and cached entries awaiting revalidation. New repository discoveries enter subsequent assessments; **they are not retroactively included in older evidence or coverage counts**. "Traversal complete" means pagination reached the end; the old "enumeration exhausted" wording did **not** mean API quota exhaustion. Incomplete passes remain visibly partial. A full pass may take multiple batches.

After a pass completes, discovery waits the normal poll interval before starting a new revalidation pass from page one **without discarding the previous inventory**. Previously cached entries may have changed access; their probes still report errors honestly. Removed repositories are pruned only after an unrestricted traversal completes. An SSO restriction on any page keeps the whole pass partial and prevents pruning based on that restricted view. Changing/removing credentials resets discovery, prior snapshots, history and check rotation; no credential identifier is exposed in the dashboard. Restarting the process also resets the cursor/cache; nothing is persisted to disk.

`IMPACT_MAX_REPOSITORIES` remains a hard capacity limit, not a bypassed safety bound. If reached, discovery keeps its unfinished page and reports `inventory_limit`; restart with a deliberately larger limit (up to 100000) to accommodate the inventory. No default cap, timeout, request budget or permission is silently increased. Old and in-progress generations each stay bounded by that limit; the displayed/checkable union is also capped. GitHub page-number pagination is not an atomic snapshot: additions/removals/renames during a long scan can shift pages. Repeated revalidation and deduplication reduce, but cannot eliminate, that uncertainty or detect silently hidden repositories.

Repositories outside a check batch rotate into later cycles. Credential changes or inventory changes can change rotation order. Assessment-row caps can also omit component pairs within a batch; all omitted counts are shown. **A partial cycle and historical samples are not a full-environment audit.** "Bounded cycle complete" means the credential-visible traversal and scheduled probes finished; it does not mean every service was tested, full account access was verified, or every Actions run/job was scanned.

## Evidence adapters and interpretation

### Past Incidents

Open **http://127.0.0.1:8765/past-incidents** or select **Past Incidents** in the navigation. Resolved/postmortem incidents are grouped by their start date, **newest first**, using your browser's local timezone. Filter by incident name, component or date. Each card links to GitHub Status and includes affected components, start/resolution timestamps and an expandable public timeline.

The monitor reads the fixed public `/api/v2/incidents.json` endpoint once after each assessment, without credentials and within that assessment client's existing request/deadline limits. This is the latest public feed only (up to 50 incidents), **not an exhaustive archive**; no older-page crawling is performed. Active incidents stay on the live page. Timelines are limited to 100 updates per incident. A failed refresh visibly retains the last timestamped history with a stale/error warning and does not invalidate the live assessment.

Environment evidence is separate from the public timeline. For up to 10 incidents, the monitor retains the **latest actual incident-time assessment sample** with per-repository classifications, reasons, links, timestamps and cycle-wide coverage/deferred counts. Samples are not cumulative across rotating batches. They remain available on the Past Incidents page once the incident appears resolved in the bounded public feed. This cache is **memory-only**, clears on restart or credential change, and does not store raw credentials. Public history is refetched on restart. An incident without retained observations explicitly shows **unknown / no incident-time assessment recorded**. The app does not reconstruct historical queue state, run retrospective probes, import the earlier one-off 37-repository investigation, or display offline test results as live evidence.

Current component names were inspected directly from the live [Status JSON API](https://www.githubstatus.com/api/v2/summary.json). The app discovers IDs/names on every poll; names select adapters and IDs correlate incidents. New/unrecognized names use an explicit unsupported adapter. The live feed currently includes the following components:

| Status component | Actual evidence | What it cannot establish |
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
| Copilot AI Model Providers | Optional manual active Copilot SDK inference; a single timestamped, requested-model client-path observation shared across selected repositories | Repository-specific or IDE impact, tenant identity, all providers/models, confirmed causation or absence of impact; defaults to unknown without configuration |
| Visit www.githubstatus.com for more information | Explicit unsupported/informational | No tenant-level service probe |

Classifications:

| Classification | Meaning |
|---|---|
| **confirmed** | Reserved for direct attributable tenant impact. **No current adapter emits this**; available evidence cannot responsibly prove GitHub causation. |
| **suspected** | A currently queued/waiting job created at least 10 minutes ago during an unresolved Actions incident, without an explicit self-hosted label. This is a correlated symptom, **not confirmation**; runner type/capacity, concurrency controls and approvals remain confounders. |
| **unknown** | Insufficient or unsupported evidence; successful metadata reads; missing access; rate limits, timeouts, service failures, historical/application failures or unproven queue timing. |
| **not-applicable** | Inventory explicitly reports Issues disabled or no configured Pages site. This is about that feature on that repository, not a component health statement. |

Actions timing uses incident `started_at` (falling back to announcement `created_at`), not titles or arbitrary failure counts. Completed runs last updated before that point are historical; active runs can overlap. Incident onset can precede announcement, so samples can miss real impact. A job's **creation age is not continuous queue duration**. Self-hosted queues remain unknown rather than being attributed to GitHub. Application test failures, timeouts and external check-app failures are recorded without assuming platform failure. Even successful incident-time runs do not establish "unaffected." Missing job creation time prevents a queue-age inference.

## Optional bounded AI incident planner

The app remains dependency-free and fully functional **without AI**. Opting in adds an untrusted, server-side planner that interprets the **already Status-mapped component category** and selects from existing read-only evidence checks. It can choose a follow-up based on the previous sanitized result: for example, sample Actions runs, inspect jobs for an incident-overlapping run, then sample that run's commit checks. It cannot infer affected components from incident titles, broaden repository scope, dispatch workflows, or decide impact.

### Provider and deployment configuration

This integration uses the current [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create) directly through Python's standard-library HTTPS client, with [strict Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). There is no SDK dependency, MCP server, provider tool access, model browsing, code execution or agent framework. Check the [model catalog](https://developers.openai.com/api/docs/models) and your project's permissions for a model supporting **Responses plus strict JSON-schema output**. There is deliberately **no default model or assumed API entitlement**. Model availability, cost, regional restrictions and schema support vary; unsupported/unavailable models fall back rather than being silently replaced.

Set these on the **local server process**, using your trusted environment/secret manager:

| Variable | Meaning |
|---|---|
| `IMPACT_AI_PROVIDER` | Unset disables AI. Only `openai` is supported. |
| `IMPACT_AI_MODEL` | Required explicit provider model ID (1..100 letters, digits, `_`, `.`, `:`, `-`). |
| `OPENAI_API_KEY` | Separate OpenAI credential; never a GitHub token. Required only with AI enabled. |

Restart `python3 run.py` after changing AI settings. `.env` is not loaded. Do not put credentials in the browser, source, command-line arguments, logs or committed files. No API keys or raw provider response/error text are returned by `/api/state` or persisted by the app. **Demo mode always disables the provider**, even if these variables are set.

Keep the dashboard loopback-only; this is not a hosted, multi-user deployment. Opt-in adds one fixed outbound origin, `https://api.openai.com/v1/responses`, to the existing GitHub destinations. The provider key is sent only to that endpoint; GitHub credentials never enter its request. Provider redirects are rejected, and custom base URLs are not accepted. The standard trusted proxy/CA caveat still applies.

### Execution and safety contract

- The host, not the model, builds the available check list from a maximum of **4 existing repository/incident/component assessment contexts** in the current rotating batch. Repository names, run IDs and commit SHAs are replaced with local aliases. Only unresolved incidents with matching live component IDs are eligible. Disabled Issues/Pages flags and unsupported adapters cannot be overridden.
- The allowlist is repository metadata, one commit, one PR, one issue, Pages configuration, Actions runs, run jobs, and commit check runs. Each matches an existing deterministic GET endpoint. Jobs are allowed only for the first 5 incident-overlapping runs returned by the current runs sample; commit checks require a validated SHA from such a run and a prior job read. Numeric run IDs, SHAs, repository paths, aliases and available operations are revalidated at execution.
- A typed plan has exactly five fields: repository alias, operation enum, target alias, rationale code, and uncertainty code. Strict JSON schema is requested, then **independently validated locally**, including rejecting extra fields, invented targets, repeated selections, wrong types and out-of-scope operations. The model cannot supply URLs, repository names, shell commands, code or arbitrary arguments. Selection rationales are intentionally constrained codes rendered as fixed prose, not free-form claims or hidden chain-of-thought.
- At most **4 model calls and 3 selected probe GET attempts per assessment cycle**, not per repository. Model output is capped at **500 tokens per call** (at most 2,000 output tokens/cycle); serialized input is capped at **16 KiB/call**, responses at **64 KiB**. There are no provider retries, conversation history IDs or unbounded loops. The planner starts no new work after **20 seconds or one quarter of the configured cycle budget**, whichever is smaller, and never beyond the existing client deadline. Each model/probe request has a socket timeout of at most **5 seconds**, further limited by the remaining budget. As with other clients, a socket timeout is not a hard wall-clock interrupt of an in-flight read.
- Planner GETs have no retries and consume the existing GitHub cycle request budget/backoff; they cannot add permissions or bypass rate limits. The single-flight scheduler plus a nonblocking planner lock prevents overlapping planner calls. Provider authentication, availability, HTTP, network and rate-limit errors suppress further provider calls for **at least 300 seconds** across refreshes. The normal assessment interval still applies. These finite caps bound requests, not a dollar price; use provider-side spend controls and choose the model deliberately.
- **Deterministic adapters and classifier always run**, including after model stop, malformed/refused output, invalid plans, unavailable models, timeout, rate limit or absent key. Successfully selected reads are reused so the baseline need not issue them again; the rest of the original deterministic checks still execute under their existing budgets. Cached read failures remain explicit unknown/errors. AI never removes baseline evidence or changes classifications. No adapter gains `confirmed` or `unaffected`.

The dashboard and `/api/state` expose `disabled`, `idle`, `completed` or `fallback` planner status, fixed error/limit codes, counts, validated selections, constrained rationales, sanitized observations and uncertainty. `completed` means only the bounded planning pass ended, **not** complete coverage or healthy service. `idle` means no eligible check in the bounded planner scope. On failure, any earlier validated selections remain visible, but subsequent deterministic-only results are explicitly labeled fallback, never presented as AI reasoning. Other repositories/components continue to receive deterministic checks; AI does not claim exhaustive coverage.

### Data sharing and limitations

Opt-in sends only component adapter/status enums, an unresolved-mapped-incident marker, ephemeral repository/run aliases, available check enums, and sanitized observations (sample/failure/queue/self-hosted counts, incident-overlapping run aliases, or a generic read-error marker). It does **not** send incident titles/bodies, repository names/URLs/metadata, code, issue/PR text, workflow/job names/logs, raw labels, run IDs, SHAs, raw API results, error messages, or either credential. The model receives no direct network tools. The dashboard can still show local repository names/evidence as before; do not share its snapshots publicly.

Requests set `store: false` and do not use server-side conversations. This is **not a promise of zero provider retention**: OpenAI's account/data-processing and abuse-monitoring policies still apply. Obtain any required approval before sharing even these aggregate signals. GitHub reads and provider results live only in process memory; raw model output is discarded after validation. No real provider inference is required by tests, and availability/quality for your configured model is not guaranteed. Without provider access the application continues deterministic monitoring with a visible fallback reason.

## Optional active Copilot model-path check

This is **separate from the AI planner** and uses the supported [GitHub Copilot SDK](https://docs.github.com/en/copilot/get-started/sdk-quickstart), not OpenAI directly or GitHub Models. The integration pins [Python SDK v1.0.29](https://github.com/github/copilot-sdk/tree/v1.0.29/python), whose `empty` mode supports explicit tool exclusion and context isolation. It is an active synthetic inference request, **not read-only**, and may consume Copilot AI credits. It does not automate your IDE. Successful SDK inference cannot establish that a VS Code or JetBrains client, another model, or every tenant user is healthy.

### Setup and manual operation

The optional adapter currently supports POSIX hosts (macOS/Linux) for process-group cancellation. Install into the same environment used to launch the server:

```sh
.venv/bin/python -m pip install '.[copilot]'
# Provision a compatible Copilot CLI/runtime using the SDK's installation guide.
# Set its absolute executable path below; this app never downloads a runtime.
export IMPACT_COPILOT_PROBE_ENABLED=1
export IMPACT_COPILOT_PROBE_MODEL=your-explicit-supported-model-id
export IMPACT_COPILOT_CLI_PATH=/absolute/path/to/compatible/copilot
# Set IMPACT_COPILOT_PROBE_TOKEN via your trusted secret manager/environment.
.venv/bin/python run.py --repos my-org/service-api
```

`IMPACT_COPILOT_PROBE_TOKEN` must be a **separate, already-authorized Copilot credential** supported by your client/account. No fallback to `GH_TOKEN`, `GITHUB_TOKEN`, `gh`, logged-in client credentials, or the planner's OpenAI key occurs. No new permissions are acquired. Do not paste credentials into the UI, shell arguments, committed files or logs. `.env` is not loaded. There is no default model and `auto` is rejected; the explicit ID must appear in the client's model list. Policy, entitlement, model availability and SDK/CLI compatibility can still reject the request. No fallback model or BYOK/custom endpoint is selected. Enterprise managed settings are requested using the session credential and locked model constraints are enforced; conflicts or policy-bootstrap failures fail rather than bypassing policy.

Open **Configuration / Copilot model-path inference**. The button is enabled only with valid server configuration, assessed repositories and a Status-mapped unresolved **Copilot AI Model Providers** incident. Press **Run one active Copilot inference check** to explicitly request a potentially billable check. It queues one fresh assessment, rechecks Status and scope, and sends nothing if the eligible incident/scope has disappeared. Normal refresh, background polling, repository selection and demo mode **never trigger inference**. The demo remains network-free. Changing model/auth settings requires a server restart.

### Limits, isolation and data sharing

- One fixed prompt (`Reply with exactly IMPACT_PROBE_OK. Do not use any tools.`), one SDK `send_and_wait` per accepted manual attempt, no application retries or adaptive follow-ups. This is diagnostic inference, not another incident-planning agent.
- At most **3 attempts per rolling hour**, shared across local app instances using the same OS home. Failed, timed-out and crashed attempts count. Reservations are committed before starting the worker to `~/.local/state/github-impact-assessment/copilot-probe.sqlite3`; only attempt timestamps are saved. A short shared lease prevents overlapping workers. Storage failure or a backward clock jump fails closed. Restarting does not reset this counter; deleting it would defeat the cap.
- **15-second worker deadline**, including SDK startup/model lookup/cleanup. An isolated POSIX process group is killed at the deadline and on worker exit, including its SDK CLI. The app does not retry timed-out requests. This bounds local execution, not completion/billing of an already accepted remote request. SDK/service internal retry behavior is not a guaranteed wire-level request count.
- These are **request/time limits, not a monetary, token or AI-credit ceiling**. The SDK does not expose a hard output-token limit here; the tiny prompt is not a token cap. Configure Copilot-side billing/budget controls for a spend ceiling before enabling this feature. A timeout may still incur charges.
- An empty temporary working/config/home directory, `mode="empty"`, replaced fixed system message, `available_tools=[]`, deny-all permission callback, no MCP/custom tools/agents/plugins/skills/hooks/custom instructions, no host Git operations, no session store or memory. Client/session telemetry and remote session export/control are disabled explicitly (`--disable-client-telemetry`, `--no-remote-export`). Server-side service telemetry is not controlled by these switches. The optional SDK and CLI are trusted local software, not an OS filesystem sandbox. Their compatibility must be maintained; unsupported SDK versions or CLI switches fail visibly. The app does not use a permissive CLI shell command or `--allow-all-tools`.
- No incident text, repository name, path, source code, workflow result or assessment data enters the prompt. The Copilot service necessarily receives authentication and standard service/account metadata plus the fixed prompt/requested model. Its applicable data-processing/retention policies still apply; this is not a zero-retention promise. SDK scratch data is deleted when the worker finishes; normal completion/timeouts do not persist credentials or transcripts. As with other temporary files, an abrupt host/process crash may leave scratch artifacts for local cleanup.

### Evidence and uncertainty

Repository Evidence shows the sample's timestamp, requested model, latency, sanitized outcome, freshness and **shared-client-path** scope. It explicitly does **not** independently verify the actual served model or the credential's enterprise/tenant membership. Use a credential appropriate to the tenant you intend to observe. This measures a configured authenticated Copilot path, not a verified whole-tenant health result.

The same sample is inherited by eligible selected repository rows; it does not pretend to run inference independently against each repository. It is reused without extra inference during polling, and is labelled stale after **5 minutes**, outside the incident window, or omitted for a different incident ID. The deterministic classifier retains **unknown** even on success or failure: success proves one response at one instant; authentication, policy, quota, network, client compatibility and provider failures cannot be distinguished reliably from a generic SDK exception. No unsupported `suspected`, `confirmed` or `unaffected` claim is introduced.

Sanitized outcomes include `success`, `unexpected_response`, `model_unavailable`, `sdk_unavailable`, `unsupported_sdk`, `client_error`, `runtime_error` and `timeout`. Raw model text, SDK logs and exception bodies are never exposed. Configuration errors, `hourly_limit`, `already_running_or_cooling_down`, `budget_storage_error` and a skipped request after a scope/incident/shutdown change remain visible in Configuration. Missing SDK/credential, policy failures and unavailable models do not prevent deterministic monitoring or the existing planner fallback.

## Architecture

```text
Browser (local static HTML/CSS/JS; no credentials)
  -> loopback Python HTTP server: GET /api/state, GET /api/past-incidents,
     POST /api/refresh, POST /api/repositories, POST /api/copilot-probe
     -> single-flight scheduler: assessments + paced discovery-only batches
        -> cached inventory + 10-entry in-memory assessment summary history
        -> public GitHub Status summary + bounded incident history GET (no auth)
        -> exact selected /repos/{owner}/{repo} validation (no enumeration)
           OR explicitly enabled credential-visible /user/repos pagination
        -> bounded concurrent GET-only evidence adapters
        -> optional bounded AI planner (fixed OpenAI Responses origin)
           -> host-validated selections + sanitized read observations
           -> reused GET evidence; deterministic classifier owns verdicts
        -> optional manual active Copilot check (separate credential)
           -> persistent attempt-only counter; isolated no-tool SDK/CLI worker
           -> shared sanitized sample; deterministic unknown repository impact
```

- `impact/client.py`: environment/`gh` credential resolution, fixed-origin requests, explicit repository-name validation, scoped metadata reads, retry/backoff, pagination and budgets.
- `impact/assessment.py`: live component mapping, conservative classifications and evidence projection.
- `impact/agent.py`: optional strict-schema provider client, scoped read allowlist, sequential planner, sanitization, budgets and explicit fallback trace.
- `impact/copilot_probe.py`: optional active Copilot SDK adapter, empty-mode isolation, manual request/time caps, local attempt ledger and sanitized shared evidence.
- `impact/monitor.py`: independent assessment/discovery schedules on one worker, cancellation, shared backoff, rotating checks and timestamped snapshots.
- `impact/history.py`: bounded public history projection and incident-date ordering; monitor retains separate incident-time evidence in memory.
- `impact/app.py` and `impact/static/`: local server and responsive dashboard.
- `run.py`: file-path launcher that resolves the bundled package independently of the current directory.
- `impact/demo.py`: network-free incident fixtures.

**No AI provider, MCP server, external database, hosted service or GitHub Actions workflow is required.** Impact classification remains deterministic Python. By default the only outbound destinations are GitHub Status and GitHub's REST API (subject to your trusted proxy settings); explicitly enabling the optional planner adds the fixed OpenAI endpoint above. Separately enabling and manually requesting the optional active check adds the supported client's Copilot service connections and an attempt-only local SQLite ledger. There are no monitored-repository mutations. A dashboard refresh is a local scheduling operation, not a GitHub write or paid inference request.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
# Replay the published November 9, 2026 Actions/Webhooks/Pages incident timeline:
python3 -m unittest tests.test_november_9_incident -v
python3 -m compileall -q impact tests
# Optional development environment for static type checking:
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m mypy impact
# Alternative type checker (requires Node.js/npm):
npm exec --yes --package=pyright -- pyright impact
# Optional JS parse check if Node.js is installed:
node --check impact/static/app.js
# Dependency-free frontend behavior tests (requires Node.js):
node --test tests/test_evidence_filter.js
# Build the installable wheel (requires pip and the declared setuptools build backend):
python3 -m pip wheel --no-deps --wheel-dir dist .
```

The offline unittest suite covers all current/future component mapping, no-incident/unknown status paths, pagination, SSO/permission/rate-limit errors, retries/budgets, incident timing, queue thresholds, self-hosting, application failure non-attribution, scheduler deduplication/periodic polling, bounded history, HTTP routes, cross-origin rejection and credential boundaries. Selection tests cover paused startup, exact-name validation, no account/organization crawl, selected-only incident checks, resumable validation, permission/identity gaps, scope transitions, bounded JSON and cross-origin rejection. Mocked planner tests cover adaptive follow-ups, malicious plans, schema/argument rejection, status/feature constraints, request/iteration/time bounds, provider errors, credential/data boundaries, read reuse and deterministic fallback. No test calls a real model. Mypy checks the Python application (including annotated and unannotated bodies); runtime guards validate external response shapes. The frontend is served directly without a JavaScript build pipeline.

`tests/fixtures/incident_2026_11_09.json` preserves the public timestamps, component IDs and status transitions of [the November 9 incident](https://www.githubstatus.com/incidents/tp8cghg7tlc4). Its source API URL is included in the fixture. The replay verifies Actions-only onset, Webhooks/Pages expansion, monitoring and resolution, with a frozen-clock assessment. **All repository/run/job evidence in these offline tests is synthetic**, not a record of your environment during the outage. These tests do not alter the live dashboard or claim historical tenant impact.

## Troubleshooting

- **`No module named impact`:** use `python3 /absolute/path/github-impact-assessment/run.py`, or change into the actual clone root before running `python3 -m impact`. If you installed the package, use that virtual environment's Python/CLI, not another interpreter. Update an older clone with `git pull --ff-only` before using the launcher. Do not copy only `run.py` or `impact/__main__.py` out of the project.
- **Authentication unknown/401:** confirm your existing `gh auth status --hostname github.com` or environment credential locally; the app re-resolves credentials each cycle. Do not paste token values into issues/logs.
- **403/404 or missing organization:** check existing SSO authorization/token repository selection and organization policy. The app will not acquire new permissions. A cooldown after 403 is intentional.
- **Partial coverage:** inspect inventory reason, deferred counts, probe errors and sample limits before making decisions. Increasing a cap does not bypass permission or GitHub rate limits.
- **Status unavailable:** inventory may still be visible, but incident state is unknown; no tenant health conclusion follows. No stale feed is relabeled current.
- **Unexpected assessment failure:** the prior snapshot is retained with a visible error kind, not raw server data. Restart, try `--demo`, and run the offline suite; investigate response-shape or runtime changes without exposing private evidence.
- **Address in use:** choose another `IMPACT_PORT`. Do not change the loopback binding to publish the dashboard.
