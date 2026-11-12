# GitHub Impact Assessment

A local **GitHub Actions and Webhooks** dashboard that polls **GitHub Status**, assesses repositories you preselect (or explicitly opt in to discover), and correlates unresolved incidents with **read-only workflow, job, commit-check and webhook delivery-summary evidence**. It never dispatches workflows, pings/redelivers webhooks or changes a monitored repository. Other GitHub services are not assessed; the active Copilot inference diagnostic and its SDK dependency have been removed.

**Unknown is not unaffected.** A public incident is not proof of your impact, and a successful metadata read is not proof that a service works. This application deliberately cannot promise definitive impact detection for every GitHub service.

## Run

Requires Python **3.11+**. The runtime and test suite use only the standard library; no dependency installation is needed.

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

### Configure writable repositories

First startup **does not populate an assessed repository inventory**. Without a saved scope or explicit startup flag, repository checks remain paused until you open **Configuration** and choose a scope in **Repositories to assess**. Organization dropdown hints load automatically through bounded membership and repository-owner metadata reads, independently of assessed scope. Later startups restore your last applied scope before assessment discovery begins. Navigation is **Live Assessment / Past Incidents / Configuration** on both pages. Switching tabs preserves unapplied repository edits and does not trigger discovery or assessment. The Configuration tab can be linked directly using `/#configuration`, and browser back/forward navigation is supported.

1. In **Repositories to Assess / Find writable repositories**, choose an **Organization or owner** from the single scrollable dropdown. Organization memberships and personal/organization owners visible through your credential populate automatically. All loaded options are available without UI truncation; this is not an unlimited API traversal. Choosing an owner automatically starts writable-catalog loading if no catalog is already available or queued, then repopulates **Available repositories** for that owner. No separate search, refresh or Add all button is needed. If a loading request fails, choose an owner again to retry; server throttling and shared rate limits still apply.
2. **Available repositories** automatically shows only that owner's verified writable repositories as catalog pages arrive. Known organization selections use paginated `GET /orgs/{organization}/repos` directly rather than waiting for an account-wide catalog to reach that organization. Personal owners use `GET /user/repos` with owned, collaborator and organization-member affiliations so private repositories remain discoverable. Results from a different catalog scope are hidden immediately. If another batch is busy or a start is throttled, the current selection retries automatically at paced intervals; shared API budgets still apply. Only literal `true` permission flags for `push`, `maintain` or `admin` qualify, so this is not a list of all repositories regardless of access. Multi-select results (Ctrl/Cmd-click or Shift) and **Add selected repositories**. Only highlighted repositories are added. Existing draft names are retained and deduplicated case-insensitively. Over-limit additions leave the draft unchanged. Multi-selections survive periodic updates while their options remain visible; changing owners clears hidden picker selections but never removes names already in the draft. With no owner selected, the repository picker is empty.
3. Alternatively enter **any exact `owner/repo` name** directly in **Selected repositories**, including personal or collaborator repos, without enumerating the account. Click **Save and verify selected repositories**. Names persist as a requested scope; they are not access approval. The scheduler validates each exact identity and its permission flags through `GET /repos/{owner}/{repo}`. Only verified writable repositories enter incident checks and continuous observations. Rejected/unknown permissions appear in Configuration's verification panel and coverage gaps; they are not silently substituted or called unaffected. Remove rejected names from the draft and save again if desired.

The **Organization or owner** dropdown is automatically populated at startup, independently of repository selection. It combines credential-visible `GET /user/orgs` membership organizations with personal and organization owners from paginated `GET /user/repos` (including outside-collaborator access). This metadata traversal retains only owner logins, never adds repositories to assessment, and runs even while repository checks are paused. It shares the existing request/time limits, single scheduler and adaptive GitHub rate budget, resumes partial pages, and refreshes hourly after completion. Each source is capped at 1,000 pages and the combined owner count at `IMPACT_MAX_REPOSITORIES`. Options are memory-only and cleared when a credential change is detected. For an owner missing from the dropdown, enter exact `owner/repo` names in Selected repositories; the same permission verification applies.

Organization listing is **credential-visible, not proof of exhaustive enterprise access**: PAT scopes, fine-grained token behavior, SSO, hidden memberships and API permissions can omit organizations. Outside-collaborator organizations require repository-owner enumeration because the memberships endpoint alone cannot discover them. Partial results, errors and rate-budget deferrals are displayed next to the dropdown. Organization membership is not approval to assess any repository; the existing writable-repository verification still applies.

The configured **server GitHub credential**, not necessarily your browser account, controls both visibility and permission evidence. Token restrictions, SSO or unsupported/missing permission metadata can hide writable repositories or prevent verification; the monitor fails closed. API-reported write access describes repository role, not a guarantee every possible write would succeed (branch rules, token scopes and organization policy still apply). **No write operation is performed to test access; no new write-capable tool or permission grant is requested.** Normal monitoring only needs the existing read endpoints.

Catalog discovery does not change or save assessed scope, start inference or automatically select results. It shares the existing resumable request/time/capacity bounds and process-wide adaptive rate budget, runs on the single scheduler, deduplicates pending/running requests and throttles starts to one per 10 seconds. Catalog/owner state is memory-only. Completion means the bounded credential-visible traversal ended, not exhaustive access across GitHub. Partial catalogs, errors and SSO restrictions remain visible. Owner selection loads the picker, not the saved assessment scope; Add selected and Save remain explicit.

**Pause repository checks** pauses the assessed scope while Status polling continues; an explicitly requested catalog may still finish. Existing explicit/saved `all` mode remains supported but now filters to credential-reported writable repositories too. Saved selected scopes/CLI names are revalidated under the same policy after upgrade; read-only/unknown names remain saved requests but are excluded from checks with explicit errors. Complete selected metadata/permissions are revalidated hourly; permissions can change between validations. Per-probe authorization errors remain coverage gaps. Catalog browsing never silently changes an existing assessment scope.


Names are deduplicated case-insensitively. URLs, paths, query strings, invalid names and selections larger than `min(IMPACT_MAX_REPOSITORIES, 200)` are rejected. **Applied scope is persisted server-side** at `~/.local/state/github-impact-assessment/repositories.json`, shared by app instances using that OS home. Only the applied mode (`selected`, `paused`, or explicitly enabled `all`) and exact selected names are stored, never credentials, evidence or unapplied drafts. Applying a selection saves it atomically before accepting the queued change; it survives restart even if the current batch has not finished. The file is created with owner-only permissions (0600 on POSIX). Repository names are private metadata: protect this local file and its backups.

Restart without flags to restore that scope. A saved `all` mode explicitly resumes account-wide discovery; saved selected names never trigger account/organization enumeration. **Pause repository checks** persists paused mode with an empty list. Startup `--repos` or `--all-repositories` overrides the saved scope for that run without replacing the file; applying a scope through the UI saves the new choice. Demo mode neither reads nor writes this file. No browser storage is used. Invalid/unreadable/oversized saved state, including selections exceeding a newly lowered limit, blocks startup with a sanitized error instead of silently crawling or dropping names. Use an explicit startup flag to recover and apply a valid scope. Save failures are shown in the UI and leave the previous active/pending scope unchanged. Multiple live app instances are last-successful-save-wins on disk, but each retains its current scope until changed or restarted.

Explicit startup overrides remain available:

```sh
python3 run.py --repos my-org/service-api my-org/web-app
# Explicit opt-in to the previous account-wide discovery behavior:
python3 run.py --all-repositories
```

`--repos` and `--all-repositories` are mutually exclusive. Changing scope queues a switch after the current bounded assessment/discovery batch finishes; the UI labels this pending state. The switch clears old inventory, evidence, summary history and rotation before scheduling the new scope. It does not interrupt an in-flight HTTP request or bypass existing time/request/backoff limits.

Selected metadata validation uses the existing paced discovery budgets and resumes at the next unfinished selected name. Missing or insufficient write-access evidence, 404/hidden or renamed repositories, invalid responses and SSO restrictions remain visible coverage gaps; they are not silently replaced or considered unaffected. Only exact matching identities with verified write permission are accepted. Failed names are retried on the normal discovery interval, while rate/time/request checkpoints retain their existing cooldown/resume behavior. A selected repository's metadata can be revalidated without scanning any organization's repository list.

To try synthetic incident paths without credentials or network requests:

```sh
python3 run.py --demo
# Or use a different port if the live monitor is already running:
IMPACT_PORT=8766 python3 run.py --demo
# Exercise explicit selection against the fixtures:
python3 run.py --demo --repos example/queue-symptom example/self-hosted
```

The demo is prominently labeled **FIXTURE DEMO**, includes an Actions queued-job symptom, a self-hosted runner confounder, webhook delivery failures without platform attribution and permission failures. Demo startup without scope flags loads all three fixture repositories, not your actual accounts; the selection interface also works with those fixture names. Its times are relative to startup; its responses are deterministic otherwise. It is not a live health assessment.

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

- Reuse existing `gh` authentication or an already available environment token. The app does not request, create, broaden, or refresh tokens/organization permissions. Repository-monitoring tokens stay server-side, in memory, and are attached **only** to GET requests to `https://api.github.com`. Redirects are rejected. The public Status API receives no credentials. The optional Actions planner uses a separate server-side credential, described below.
- Explicit assessment selection reads only exact selected repository metadata and incident evidence. Separately, organization dropdown hints automatically enumerate bounded membership and repository-owner metadata; they never expand assessed scope. Optional account-wide assessment discovery paginates `GET /user/repos` with owner, collaborator and organization-member affiliations. This is the meaningful enumerable "all accessible repositories" scope, **not all public repositories on GitHub**, and not a guarantee of all repositories your user account can access. Fine-grained token selection, GitHub App identity, organization policy, SSO and permissions can silently hide repositories. Every dashboard snapshot carries its scope/access caveat. This version targets **github.com**, not GHES or `.ghe.com`.
- Existing credentials must have access to the relevant repositories. Depending on token type, the probes need read access to repository metadata, Actions runs/jobs, Checks and repository webhooks. Webhook listing/delivery-summary endpoints may require repository admin access and webhook permissions not present in the existing credential. Missing access produces an explicit unknown/error; do not grant admin or write permissions just to improve this dashboard. Classic PATs/OAuth tokens may have broader scopes than needed; the application still sends GET only.
- An SSO `partial-results` response marks enumeration partial. A 403 is labeled permission-or-secondary-limit (GitHub does not always distinguish these via headers), and a 404 can mean absent **or hidden**. SSO can also silently hide entire organizations. Authentication failures, service errors and rate limits never count as confirmed impact.
- No request logs, workflow logs, webhook payloads, private code, repository secrets or raw error response bodies are collected. Webhook listing responses include configuration fields in process memory; receiver URLs, configuration, secrets, free-text responses and delivery GUIDs are never projected into evidence, logs, storage or the optional AI planner. Delivery-detail/payload endpoints are never called. Evidence is reduced to names, timestamps, status/conclusions, counts and links. Repository names/evidence remain private data: do not expose this port, reverse proxy it, or share its API responses publicly. Any local user/process with access to loopback can read the dashboard; it is not an authenticated multi-user server.
- No browser token, CORS, external scripts/fonts, application telemetry, browser storage, or persistent assessment file. Strict Host/Origin checks mitigate cross-site refresh and DNS rebinding; a CSP blocks remote assets and framing. Browsing evidence links is user-initiated with no referrer. History contains at most 10 summary snapshots **in server memory**, cleared on restart. The opt-in active check persists only its attempt timestamps for the hourly cap, not credentials or evidence.
- The app honors the Python runtime's proxy/CA configuration. Use only a trusted local machine, proxy and Python/`gh` installation. `.env` and generated Python/package output are ignored by git. `.env.example` documents variables; the app **does not automatically load `.env` files**. Never put tokens on command lines, in source, in URLs, or in this README.

## Monitoring and bounded coverage

Startup polls Status with repository work paused unless a saved or explicit startup scope is configured (demo defaults to fixture scope). With a scope enabled, startup performs one short selected-validation or account-discovery batch, then the first assessment. Later assessments read the latest scoped inventory snapshot without waiting for enumeration. Repository discovery/validation has its own paced background schedule. **Continuous Actions observations run for explicitly selected repositories even without incidents; webhook checks are incident-triggered only.** Incident-specific checks run only for unresolved incidents mapped to live Actions or Webhooks component IDs. Mixed-service incidents retain their public title/timeline but only these two services are assessed. Incidents/components affecting neither are omitted from live and past assessment views. A degraded supported component without a mapped incident is flagged; incident scope is never guessed from titles. Missing supported components are explicit coverage gaps.

The dashboard offers manual refresh, incident/component cards, per-repository evidence and error reasons, inventory, filtering, sample limits, timestamps, freshness, and in-memory history. It polls the local server every 5 seconds; that does not cause a GitHub poll. Only one worker performs network work: due assessments take priority over discovery batches, so they never overlap or multiply request concurrency. A running discovery batch may delay an assessment until the batch yields. Manual assessment requests deduplicate, with a 10-second throttle. Periodic assessment polling is measured **after the previous assessment completes**. Discovery-only batches do not rerun incident probes, advance check rotation, or add history entries. Exceptions retain the previous timestamped snapshot with a visible failure/staleness warning.

Environment settings (strictly validated integers):

| Variable | Default | Bounds / meaning |
|---|---:|---|
| `IMPACT_POLL_SECONDS` | 300 | 60..86400; delay after each completed cycle |
| `IMPACT_TIMEOUT_SECONDS` | 10 | 1..60; socket timeout for each HTTP attempt |
| `IMPACT_CYCLE_SECONDS` | 120 | 10..1800; cycle deadline, stops starting requests |
| `IMPACT_MAX_REQUESTS` | 500 | 1..10000; all Status/GitHub attempts, including retries, per cycle |
| `IMPACT_GITHUB_REQUESTS_HOUR` | 600 | 1..1800; rolling hourly GitHub attempt ceiling shared by all clients in this process |
| `IMPACT_GITHUB_REQUESTS_MINUTE` | 30 | 1..60; rolling minute GitHub attempt ceiling shared by all clients in this process |
| `IMPACT_MAX_REPOSITORIES` | 10000 | 1..100000; inventory cap, reported as partial when exceeded |
| `IMPACT_MAX_CHECK_REPOSITORIES` | 200 | 1..10000; rotating incident-time repository batch |
| `IMPACT_MAX_ASSESSMENTS` | 2000 | 1..20000; total repository/incident/component rows per cycle |
| `IMPACT_DISCOVERY_SECONDS` | 15 | 1..60; time budget per discovery-only batch |
| `IMPACT_DISCOVERY_REQUESTS` | 10 | 1..100; attempts per discovery-only batch, also capped by `IMPACT_MAX_REQUESTS` |
| `IMPACT_DISCOVERY_RESUME_SECONDS` | 5 | 1..300; pause after a discovery time/request checkpoint before the next batch |
| `IMPACT_WORKERS` | 4 | 1..16; bounded simultaneous repository probes |
| `IMPACT_PORT` | 8765 | 1024..65535; loopback dashboard port |

Example: `IMPACT_MAX_CHECK_REPOSITORIES=50 IMPACT_POLL_SECONDS=600 python3 -m impact`.

Enumeration uses pages of 100. By default, each background batch starts at most 10 HTTP attempts and runs for at most 15 seconds before checkpointing; a checkpoint resumes after 5 seconds, not after the 300-second assessment poll. Discovery has its own client/batch budget, **but every GitHub attempt also consumes the same process-wide minute/hour budget**, including assessment, organization discovery, planner checks, continuous observations and retries. Complete selected metadata is revalidated no more often than hourly to avoid repeatedly spending quota on unchanged identities. Failed/incomplete validation still uses existing retry/checkpoint behavior. Assessment clients retain `IMPACT_MAX_REQUESTS` and `IMPACT_CYCLE_SECONDS`; the standalone `assess()` fallback without a supplied inventory snapshot retains its half-cycle inventory budget.

An in-flight read can overrun a phase's wall-clock budget, but clients do not start attempts past their deadline. Responses are capped at 8 MiB each. Transient network errors and HTTP 5xx retry at most twice (1s/2s cancellable waits), within all request/time budgets. HTTP 401/404/other 4xx are not retried. Successful responses' rate-limit headers proactively pause GitHub reads when remaining quota reaches a reserve of 10% of the reported limit, capped at 50 requests (at least 1), until reset plus 2 seconds. Concurrent/out-of-order responses cannot raise the remaining estimate within one provider quota window. Rate-limit responses set at least a 60-second pause, honoring numeric or HTTP-date Retry-After and quota reset. A 403 without definitive headers also conservatively sets a cooldown because it could be a secondary limit. Neither manual refresh nor checkpoint continuation bypasses any of these limits. Public Status requests remain independent of the GitHub request budget/cooldown.

The dashboard displays budget usage, quota estimate and deferral reason/duration. **No client can guarantee never receiving a primary/secondary limit:** other applications can spend the credential's quota, GitHub can change secondary limits, and headers can be absent or delayed. Run one monitor process per credential; local rolling counters are process-local and reset at restart (provider headers are relearned on the next response). This implementation bounds its own attempts and honors observed limits rather than promising immunity. Deferral sacrifices freshness, never changes scope or manufactures successful evidence.

The deadline prevents new network attempts, not instantaneous interruption of in-flight socket I/O. Shutdown cancels pending starts/backoff and waits briefly for in-flight reads. Offline, permission, API errors and budget exhaustion remain visible as unknown/partial; they never become an all-clear.

### Continuous observations, baselines and durable evidence

For **selected** scope, Live Assessment includes a continuous observation panel with one row per explicitly selected repository, including repositories whose metadata could not be validated. All-repository discovery and paused modes deliberately do not enable continuous sampling. This is not enterprise-wide inventory verification.

- Sample at most **20 due repositories per assessment cycle**, prioritizing reusable incident-time run-list reads and otherwise the oldest attempt first; fresh attempts wait at least the poll interval. No repository discovery or new scope is inferred from results. Per-cycle request/time bounds and the shared rate budget can stop a batch early. Incident-time read results are reused within the cycle; they are not reused as new observations across cycles.
- Read only the **30 most recently created Actions runs** and retain aggregate run counts, queued/waiting counts, completed/failure counts and oldest queued creation age. No workflow dispatch, logs, source code, job payloads or synthetic writes. A 404/403/timeout is a coverage gap, not evidence that Actions is disabled or impacted. An empty sample is not functional verification.
- Compare queued-run counts against the median of up to **12 prior eligible samples within 24 hours**, requiring at least **3**. Eligible means the Actions component was reported operational with no mapped Actions incident at collection, not verified healthy behavior. For an active Actions incident, only samples strictly before its reported start are eligible; missing start times disable comparison. At least 3 extra queued runs versus baseline plus a queued creation age of at least 10 minutes yields **elevated_queue_sample**, never confirmed impact. Run failures remain descriptive counts, not a population failure rate.
- Mark missing/failed/older-than-**15-minute** observations as gaps; indicate insufficient baseline and record incident overlap separately from observed symptoms. A larger selected scope, long poll interval, many incidents or constrained budget can make this freshness target infeasible. For 200 repos with the default 20-per-5-minute-cycle cap, one sweep alone takes roughly 50 minutes plus cycle time; the UI reports stale gaps rather than claiming reliable fresh coverage. Reduce selected scope or intentionally adjust polling within budget if freshness is important. Budget exhaustion also advances incident-check rotation so the first repositories cannot monopolize every poll.
- Live startup stores sanitized aggregate observations in `~/.local/state/github-impact-assessment/observations.sqlite3` (0600 on POSIX). Retention is **14 days and at most 20,000 samples across scopes**, cleaned at startup/writes; this is logical retention, not secure disk erasure. No raw API responses, credentials, repository contents, run titles/IDs, model text or error bodies are saved. Exact repository names, timestamps, aggregate counts and bounded incident IDs are private metadata: protect this database and its backups. Demo/test monitoring is memory-only unless a test explicitly injects a temporary store.
- Samples are partitioned by a non-exported hash of the credential fingerprint and exact case-insensitive selected set. Restarting with the same credential/scope restores comparisons; changing either does not expose or mix another scope's history. Pausing stops sampling, not historical retention. The dashboard shows up to 3 retained incident samples per selected repo after resolution, in addition to the latest observation. Detailed incident assessment tables and public history remain memory-only; no retrospective tests or evidence for offline/sleep periods are invented.

Database startup failures block startup with a sanitized configuration error. Runtime write/read failures leave incident checks functioning but explicitly mark continuous observation coverage unavailable. **Other services are outside scope.** The model cannot promote these observations into a confirmed verdict. This provides Actions symptom comparison, not complete functional or causal verification.

### Filtering repository evidence

The **Repository** dropdown in **Repository evidence** lists repositories with results in the current assessment. Select one repository or **All repositories**, and optionally combine it with the text filter. The selection survives dashboard refreshes while that repository still has results; it resets to all repositories if the repository leaves the assessment. This is a local display filter only: it does not change monitoring scope or trigger repository discovery/probes. Use **Repositories to assess** in the **Configuration** tab to change the assessed scope.

### Workaround for large inventories: resumable discovery

This section describes opt-in **account-wide** discovery. Prefer explicit selection above if only a few repositories need monitoring.

Discovery **continues at the next unfinished page** after time/request limits, transient failures, or rate limits rather than restarting at page one. Each background batch adds to a bounded, memory-only inventory. **`inventory_deadline` is now a resumable checkpoint, not a five-minute stall:** the scheduler queues another discovery-only batch after `IMPACT_DISCOVERY_RESUME_SECONDS`. The coverage panel labels expected time/request checkpoints as **Checkpoint saved**, while genuine errors remain visible. Manual refresh reads the cache instead of restarting enumeration and respects the same backoff.

The coverage panel shows live background inventory progress separately from the last assessment's inventory/counts. It includes the next batch time, next page, current-pass count/start time, last unrestricted completion time, and cached entries awaiting revalidation. New repository discoveries enter subsequent assessments; **they are not retroactively included in older evidence or coverage counts**. "Traversal complete" means pagination reached the end; the old "enumeration exhausted" wording did **not** mean API quota exhaustion. Incomplete passes remain visibly partial. A full pass may take multiple batches.

After a complete account-wide pass, discovery waits the normal poll interval before starting a new pass from page one without discarding the previous inventory. Previously cached entries may have changed access; their probes still report errors honestly. Removed repositories are pruned only after an unrestricted traversal completes. An SSO restriction on any page keeps the whole pass partial and prevents pruning based on that restricted view. Changing/removing credentials resets discovery, prior detailed snapshots/history and check rotation; no credential identifier is exposed in the dashboard. Restarting resets the cursor/cache. Applied selection and the separately bounded aggregate observation store persist as documented above.

`IMPACT_MAX_REPOSITORIES` remains a hard capacity limit, not a bypassed safety bound. If reached, discovery keeps its unfinished page and reports `inventory_limit`; restart with a deliberately larger limit (up to 100000) to accommodate the inventory. No default cap, timeout, request budget or permission is silently increased. Old and in-progress generations each stay bounded by that limit; the displayed/checkable union is also capped. GitHub page-number pagination is not an atomic snapshot: additions/removals/renames during a long scan can shift pages. Repeated revalidation and deduplication reduce, but cannot eliminate, that uncertainty or detect silently hidden repositories.

Repositories outside a check batch rotate into later cycles. Credential changes or inventory changes can change rotation order. Assessment-row caps can also omit component pairs within a batch; all omitted counts are shown. **A partial cycle and historical samples are not a full-environment audit.** "Bounded cycle complete" means the credential-visible traversal and scheduled probes finished; it does not mean every service was tested, full account access was verified, or every Actions run/job was scanned.

## Evidence adapters and interpretation

### Past Incidents

Open **http://127.0.0.1:8765/past-incidents** or select **Past Incidents** in the navigation. Only resolved/postmortem incidents whose public components include Actions or Webhooks are shown, grouped by start date, **newest first**, using your browser's local timezone. Each card links to GitHub Status and includes the supported components, start/resolution timestamps and the public timeline. Mixed-service titles/timeline text are not rewritten; other services are not assessed.

The monitor reads the fixed public `/api/v2/incidents.json` endpoint once after each assessment, without credentials and within that assessment client's existing request/deadline limits. This is the latest public feed only (up to 50 incidents), **not an exhaustive archive**; no older-page crawling is performed. Active incidents stay on the live page. Timelines are limited to 100 updates per incident. A failed refresh visibly retains the last timestamped history with a stale/error warning and does not invalidate the live assessment.

Environment evidence is separate from the public timeline. For up to 10 incidents, the monitor retains the **latest actual incident-time assessment sample** with per-repository classifications, reasons, links, timestamps and cycle-wide coverage/deferred counts. Samples are not cumulative across rotating batches. They remain available on the Past Incidents page once the incident appears resolved in the bounded public feed. This cache is **memory-only**, clears on restart or credential change, and does not store raw credentials. Public history is refetched on restart. An incident without retained observations explicitly shows **unknown / no incident-time assessment recorded**. The app does not reconstruct historical queue state, run retrospective probes, import the earlier one-off 37-repository investigation, or display offline test results as live evidence.

The app discovers Actions and Webhooks component IDs from the live [Status JSON API](https://www.githubstatus.com/api/v2/summary.json) on every poll and maps incidents using those IDs, never incident titles. Other component names do not enable probes.

| Status component | Actual evidence | What it cannot establish |
|---|---|---|
| Actions | 30 most recent runs; first 100 jobs and first 100 commit check runs for up to 5 incident-overlapping runs per repository/incident | Exhaustive history, execution, dispatch, runner connectivity/capacity, log diagnosis, or absence of impact |
| Webhooks | First 3 repository hooks via `GET /repos/{owner}/{repo}/hooks?per_page=3&page=1`, then first 30 [delivery summaries](https://docs.github.com/en/rest/repos/webhooks#list-deliveries-for-a-repository-webhook) per active hook; incident-time attempt/failure counts | Organization/GitHub App hooks, exhaustive history, event-generation gaps, queue/delivery latency, receiver health, payload correctness or GitHub causation. No receiver requests, delivery-detail/payload reads, pings or redeliveries. |

Webhook samples are explicitly first-page-only (at most four logical reads per repository/incident); a full three-hook page is marked potentially partial. Transport retries still consume the existing shared request/time/rate budgets. No automatic pagination or permission escalation is performed. Failed or no-response attempts during the incident remain **unknown**, with receiver/network/configuration confounders shown. Historical/future attempts are not incident evidence; successful, empty, inactive or missing samples never establish unaffected status. Delivery attempt timestamps are not event creation timestamps and cannot measure dispatch delay. Continuous persistence/baselines remain **Actions-only**; webhook evidence is in memory only.

Classifications:

The dashboard uses traffic-light colors on repository assessments, component status, public incident cards and continuous observation signals. **Yellow** indicates degradation: a suspected Actions queue symptom, incident-time webhook delivery failures, a fresh elevated-queue observation, or public Status `degraded_performance` / minor incident impact. **Red** indicates a likely outage: public Status `partial_outage` / `major_outage`, major/critical public incident impact, or an explicitly outage/confirmed assessment (no current tenant adapter emits one). **Green** is the remainder, including unknown, inaccessible, missing or stale evidence, and means **no degradation signal established**, not healthy/unaffected. Classification, access errors, freshness and limitations remain visible independently of color. Public Status colors describe the platform's report, never proof of tenant impact; failed application tests do not become red assessments. The structured assessment `severity` field (`none`, `degraded`; `outage` reserved) is separate from impact classification. Webhook delivery failures stay **unknown** even when colored yellow.

| Classification | Meaning |
|---|---|
| **confirmed** | Reserved for direct attributable tenant impact. **No current adapter emits this**; available evidence cannot responsibly prove GitHub causation. |
| **suspected** | A currently queued/waiting job created at least 10 minutes ago during an unresolved Actions incident, without an explicit self-hosted label. This is a correlated symptom, **not confirmation**; runner type/capacity, concurrency controls and approvals remain confounders. |
| **unknown** | Insufficient or unsupported evidence; successful metadata reads; missing access; rate limits, timeouts, service failures, historical/application failures or unproven queue timing. |
| **not-applicable** | Reserved for compatibility; Actions checks do not emit this classification. |

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

- The host, not the model, builds the available check list from a maximum of **4 existing repository/incident/Actions assessment contexts** in the current rotating batch. Repository names, run IDs and commit SHAs are replaced with local aliases. Only unresolved incidents with matching live Actions component IDs are eligible. Other services cannot be selected.
- The allowlist is Actions runs, run jobs, and commit check runs (plus stop). Each matches an existing deterministic GET endpoint. Jobs are allowed only for the first 5 incident-overlapping runs returned by the current runs sample; commit checks require a validated SHA from such a run and a prior job read. Numeric run IDs, SHAs, repository paths, aliases and available operations are revalidated at execution.
- A typed plan has exactly five fields: repository alias, operation enum, target alias, rationale code, and uncertainty code. Strict JSON schema is requested, then **independently validated locally**, including rejecting extra fields, invented targets, repeated selections, wrong types and out-of-scope operations. The model cannot supply URLs, repository names, shell commands, code or arbitrary arguments. Selection rationales are intentionally constrained codes rendered as fixed prose, not free-form claims or hidden chain-of-thought.
- At most **4 model calls and 3 selected probe GET attempts per assessment cycle**, not per repository. Model output is capped at **500 tokens per call** (at most 2,000 output tokens/cycle); serialized input is capped at **16 KiB/call**, responses at **64 KiB**. There are no provider retries, conversation history IDs or unbounded loops. The planner starts no new work after **20 seconds or one quarter of the configured cycle budget**, whichever is smaller, and never beyond the existing client deadline. Each model/probe request has a socket timeout of at most **5 seconds**, further limited by the remaining budget. As with other clients, a socket timeout is not a hard wall-clock interrupt of an in-flight read.
- Planner GETs have no retries and consume the existing GitHub cycle request budget/backoff; they cannot add permissions or bypass rate limits. The single-flight scheduler plus a nonblocking planner lock prevents overlapping planner calls. Provider authentication, availability, HTTP, network and rate-limit errors suppress further provider calls for **at least 300 seconds** across refreshes. The normal assessment interval still applies. These finite caps bound requests, not a dollar price; use provider-side spend controls and choose the model deliberately.
- **Deterministic adapters and classifier always run**, including after model stop, malformed/refused output, invalid plans, unavailable models, timeout, rate limit or absent key. Successfully selected reads are reused so the baseline need not issue them again; the rest of the original deterministic checks still execute under their existing budgets. Cached read failures remain explicit unknown/errors. AI never removes baseline evidence or changes classifications. No adapter gains `confirmed` or `unaffected`.

The dashboard and `/api/state` expose `disabled`, `idle`, `completed` or `fallback` planner status, fixed error/limit codes, counts, validated selections, constrained rationales, sanitized observations and uncertainty. `completed` means only the bounded planning pass ended, **not** complete coverage or healthy service. `idle` means no eligible check in the bounded planner scope. On failure, any earlier validated selections remain visible, but subsequent deterministic-only results are explicitly labeled fallback, never presented as AI reasoning. Other repositories/components continue to receive deterministic checks; AI does not claim exhaustive coverage.

### Data sharing and limitations

Opt-in sends only component adapter/status enums, an unresolved-mapped-incident marker, ephemeral repository/run aliases, available check enums, and sanitized observations (sample/failure/queue/self-hosted counts, incident-overlapping run aliases, or a generic read-error marker). It does **not** send incident titles/bodies, repository names/URLs/metadata, code, issue/PR text, workflow/job names/logs, raw labels, run IDs, SHAs, raw API results, error messages, or either credential. The model receives no direct network tools. The dashboard can still show local repository names/evidence as before; do not share its snapshots publicly.

Requests set `store: false` and do not use server-side conversations. This is **not a promise of zero provider retention**: OpenAI's account/data-processing and abuse-monitoring policies still apply. Obtain any required approval before sharing even these aggregate signals. GitHub reads and provider results live only in process memory; raw model output is discarded after validation. No real provider inference is required by tests, and availability/quality for your configured model is not guaranteed. Without provider access the application continues deterministic monitoring with a visible fallback reason.

## Architecture

```text
Browser (local static HTML/CSS/JS; no credentials)
  -> loopback Python HTTP server: GET /api/state, GET /api/past-incidents,
     POST /api/refresh, POST /api/repositories, POST /api/writable-repositories,
     POST /api/organization (legacy catalog)
     -> single-flight scheduler: assessments + paced discovery-only batches
        -> cached inventory + 10-entry in-memory assessment summary history
        -> public GitHub Status summary + bounded incident history GET (no auth)
        -> exact selected /repos/{owner}/{repo} validation (no enumeration)
           OR explicitly enabled credential-visible /user/repos pagination
        -> bounded concurrent GET-only Actions and webhook-summary evidence checks
        -> optional bounded AI planner (fixed OpenAI Responses origin)
           -> host-validated selections + sanitized read observations
           -> reused GET evidence; deterministic classifier owns verdicts
```

- `impact/client.py`: environment/`gh` credential resolution, fixed-origin requests, explicit repository-name validation, scoped metadata reads, retry/backoff, pagination and budgets.
- `impact/assessment.py`: live component mapping, conservative classifications and evidence projection.
- `impact/agent.py`: optional strict-schema provider client, scoped read allowlist, sequential planner, sanitization, budgets and explicit fallback trace.
- `impact/monitor.py`: independent assessment/discovery schedules on one worker, cancellation, shared backoff, rotating checks and timestamped snapshots.
- `impact/selection_store.py`: bounded saved-scope validation and atomic owner-only local configuration writes; no credentials or assessment data.
- `impact/history.py`: bounded public history projection and incident-date ordering; monitor retains separate incident-time evidence in memory.
- `impact/observations.py`: bounded selected-repository Actions aggregates, preincident baseline comparisons and credential/scope-isolated local SQLite retention.
- `impact/app.py` and `impact/static/`: local server and responsive dashboard.
- `run.py`: file-path launcher that resolves the bundled package independently of the current directory.
- `impact/demo.py`: network-free incident fixtures.

**No AI provider, MCP server, external database, hosted service or GitHub Actions workflow is required.** Impact classification remains deterministic Python. By default the only outbound destinations are GitHub Status and GitHub's REST API (subject to your trusted proxy settings); explicitly enabling the optional Actions planner adds the fixed OpenAI endpoint above. There are no monitored-repository mutations. A dashboard refresh is a local scheduling operation, not a GitHub write.

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

`tests/fixtures/incident_2026_11_09.json` preserves the public timestamps, component IDs and status transitions of [the November 9 incident](https://www.githubstatus.com/incidents/tp8cghg7tlc4). Its source API URL is included in the fixture. The frozen-clock replay verifies Actions and Webhooks mapping through the incident's Webhooks/Pages expansion, monitoring and resolution while Pages remains excluded. Webhook tests cover timing, bounds, permissions, malformed responses, non-attribution, sensitive-field exclusion and summary-only paths. **All repository/run/job/webhook evidence in these offline tests is synthetic**, not a record of your environment during the outage. These tests do not alter the live dashboard or claim historical tenant impact.

## Troubleshooting

- **`No module named impact`:** use `python3 /absolute/path/github-impact-assessment/run.py`, or change into the actual clone root before running `python3 -m impact`. If you installed the package, use that virtual environment's Python/CLI, not another interpreter. Update an older clone with `git pull --ff-only` before using the launcher. Do not copy only `run.py` or `impact/__main__.py` out of the project.
- **Authentication unknown/401:** confirm your existing `gh auth status --hostname github.com` or environment credential locally; the app re-resolves credentials each cycle. Do not paste token values into issues/logs.
- **403/404 or missing organization:** check existing SSO authorization/token repository selection and organization policy. The app will not acquire new permissions. A cooldown after 403 is intentional.
- **Partial coverage:** inspect inventory reason, deferred counts, probe errors and sample limits before making decisions. Increasing a cap does not bypass permission or GitHub rate limits.
- **Status unavailable:** inventory may still be visible, but incident state is unknown; no tenant health conclusion follows. No stale feed is relabeled current.
- **Unexpected assessment failure:** the prior snapshot is retained with a visible error kind, not raw server data. Restart, try `--demo`, and run the offline suite; investigate response-shape or runtime changes without exposing private evidence.
- **Address in use:** choose another `IMPACT_PORT`. Do not change the loopback binding to publish the dashboard.
