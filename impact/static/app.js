"use strict";
let state = null;
let visible = 50;
let loading = false;
let actionMessage = "";
let selectionDirty = false;
let selectionSaving = false;
let selectionMessage = "";
let copilotMessage = "";
let copilotSubmitting = false;
function renderPage() {
  const configuration = window.location.hash === "#configuration";
  $("configuration-panel").hidden = !configuration;
  $("dashboard-panel").hidden = configuration;
  for (const [id, active] of [["configuration-tab", configuration], ["dashboard-tab", !configuration]]) {
    if (active) $(id).setAttribute("aria-current", "page");
    else $(id).removeAttribute("aria-current");
  }
}
function selectedRepositoryNames() {
  return $("repository-names").value.split(/[\s,]+/).filter(Boolean);
}
function renderAssessedRepositoryDropdown() {
  const dropdown = $("assessed-repository");
  const organization = $("repository-organization").value.trim().toLowerCase();
  const query = $("repository-search").value.trim().toLowerCase();
  const discovery = state?.organization_discovery;
  const matches = discovery?.writable === true;
  const known = matches ? discovery.latest?.repositories || [] : [];
  const repositories = [...new Set(known.filter(repo => repo.write_access === true).map(repo => repo.name)
    .filter(name => (!organization || name.split("/")[0].toLowerCase() === organization)
      && name.toLowerCase().includes(query)))].sort((a, b) => a.localeCompare(b));
  const values = repositories;
  const selected = new Set([...dropdown.options].filter(option => option.selected).map(option => option.value));
  if (JSON.stringify([...dropdown.options].map(option => option.value)) !== JSON.stringify(values)) {
    dropdown.replaceChildren(...values.map((name) => {
      const option = node("option", name);
      option.value = name;
      option.selected = selected.has(name);
      return option;
    }));
  }
  dropdown.disabled = !matches || !repositories.length || selectionSaving || Boolean(state?.stopped);
  $("select-all-repositories").disabled = dropdown.disabled;
  $("add-selected-repositories").disabled = dropdown.disabled || ![...dropdown.options].some(option => option.selected);
  $("discover-organization").disabled = selectionSaving || Boolean(state?.stopped) || Boolean(discovery?.busy);
  $("assessed-repository-help").textContent = matches
    ? [
      `${repositories.length} writable repositories match your filters.`,
      discovery.busy || discovery.pending ? "Discovery queued/running or waiting for continuation." : "",
      discovery.error?.message || discovery.latest?.reason?.message,
      discovery.latest?.complete ? "Bounded writable catalog traversal complete; credential/SSO restrictions may still hide repositories." : "Coverage partial or pending; missing repositories are not unaffected.",
      "Ctrl/Cmd-click or Shift to multi-select, then Add selected repositories.",
    ].filter(Boolean).join("\n")
    : "Find my writable repositories to browse personal, organization and collaborator access, or enter exact owner/repo names below. No discovery is required for manual entry.";
  const owners = new Set([
    ...(state?.discovery?.latest?.repositories || []).map(repo => repo.name.split("/")[0]),
    ...(state?.selection?.repositories || []).map(name => name.split("/")[0]),
    ...selectedRepositoryNames().map(name => name.split("/")[0]),
    ...known.map(repo => repo.name.split("/")[0]),
  ]);
  $("repository-organizations").replaceChildren(...[...owners].sort().map(owner => {
    const option = node("option", owner); option.value = owner; return option;
  }));
}
function renderCopilotProbe() {
  const probe = state?.copilot_probe;
  const scoped = (state?.latest?.checks || []).some(row => row.adapter === "copilot_model");
  $("copilot-probe").disabled = !probe || probe.configuration !== "ready" || !scoped
    || state.busy || state.refresh_pending || state.stopped || state.demo
    || Boolean(state.selection?.pending) || copilotSubmitting;
  const sample = probe?.sample;
  const age = sample ? (Date.now() - new Date(sample.checked_at)) / 1000 : Infinity;
  $("copilot-probe-status").textContent = [
    `Configuration: ${probe?.configuration || "disabled"}. Last request: ${probe?.status || "not run"}.`,
    probe?.requested_model ? `Requested model: ${probe.requested_model}.` : "",
    sample ? `Shared sample: ${sample.outcome}, ${sample.latency_ms} ms at ${date(sample.checked_at)}${age > 300 ? " (STALE)" : ""}. Actual served model not independently verified.` : "No active inference evidence.",
    scoped ? "Results appear in Repository evidence after the assessment finishes." : "No scoped model-provider incident; active check unavailable.",
    copilotMessage,
  ].filter(Boolean).join(" ");
}
function renderSelection() {
  const selection = state.selection;
  if (!selection) return;
  if (!selectionDirty && document.activeElement !== $("repository-names")) {
    $("repository-names").value = (selection.pending?.repositories || selection.repositories).join("\n");
  }
  const description = selection.mode === "paused"
    ? "Repository checks paused. No repository discovery requests. Choose repositories to begin; public Status polling continues."
    : selection.mode === "selected"
      ? `Saved selection: ${selection.repositories.length} repositories; only verified write-access repositories are assessed. No account-wide crawl.`
      : "Account-wide discovery is enabled.";
  $("selection-status").textContent = [
    description,
    selection.pending ? `Scope change queued (${selection.pending.mode}); applies after the running assessment/discovery batch finishes.` : "",
    selectionMessage,
  ].filter(Boolean).join(" ");
  $("selection-help").textContent = `Up to ${selection.limit} exact owner/repo names. ${selection.persistent
    ? "Applied scope is saved on this server and restored after restart; unapplied drafts are not saved. Startup --repos or --all-repositories overrides saved scope."
    : "This demo/test selection is memory-only and clears on restart."} Saved names are requests, not access approval: server-side validation excludes read-only, unknown or revoked permissions.`;
  const validation = state.discovery?.latest;
  const validationPanel = $("selection-validation");
  validationPanel.replaceChildren();
  const verified = validation?.repositories?.filter(repo => repo.write_access === true) || [];
  paragraph(validationPanel, `Verified writable repositories: ${verified.length}. ${state.discovery?.busy ? "Verification running." : validation ? "Latest server validation shown below." : "Awaiting server validation."} Saved names are assessed only after permission verification.`);
  for (const repo of verified) paragraph(validationPanel, `${repo.name} — write access verified${repo.archived ? "; archived repository" : ""}.`);
  for (const error of validation?.repository_errors || []) {
    paragraph(validationPanel, `${error.repository}: ${error.kind} — ${error.message}`, "error");
  }
  if (validation && !validation.complete && validation.reason) paragraph(validationPanel, `Partial verification: ${validation.reason.message}`, "warning");
  paragraph(validationPanel, "Permissions reflect the configured GitHub credential, not necessarily your browser account. Rechecked during metadata validation; complete selections are revalidated hourly. Revocations or credential restrictions can produce coverage gaps.", "muted");
  for (const id of ["apply-repositories", "pause-repositories", "discover-organization"]) {
    $(id).disabled = selectionSaving || state.stopped;
  }
  renderAssessedRepositoryDropdown();
}
function renderChecks() {
  const dropdown = $("evidence-repository");
  const selected = dropdown.value;
  const all = state?.latest?.checks || [];
  const repositories = [...new Set(all.map((row) => row.repository))].sort((a, b) => a.localeCompare(b));
  const options = ["", ...repositories];
  if (JSON.stringify([...dropdown.options].map((option) => option.value)) !== JSON.stringify(options)) {
    dropdown.replaceChildren(...options.map((repository) => {
      const option = node("option", repository || "All repositories");
      option.value = repository;
      return option;
    }));
    dropdown.value = repositories.includes(selected) ? selected : "";
    if (dropdown.value !== selected) visible = 50;
  }
  dropdown.disabled = !repositories.length;
  if (!state?.latest) return;
  const query = $("filter").value.trim().toLowerCase();
  const rows = all.filter((row) => (!dropdown.value || row.repository === dropdown.value)
    && `${row.repository} ${row.component} ${row.classification} ${row.reason}`.toLowerCase().includes(query));
  $("checks").replaceChildren();
  for (const row of rows.slice(0, visible)) {
    const tr = node("tr");
    const repo = node("td");
    repo.append(link(row.repository, row.repository_url));
    paragraph(repo, row.component);
    paragraph(repo, row.incident, "muted");
    const classification = node("td");
    classification.append(badge(row.classification));
    paragraph(classification, row.reason);
    if (row.error_kind) paragraph(classification, `Probe error: ${row.error_kind}`, "error");
    const evidence = node("td");
    for (const item of row.evidence) {
      const p = node("p");
      p.append(link(item.summary, item.url));
      evidence.append(p);
    }
    const details = node("details");
    details.append(node("summary", "Evidence limitations"));
    paragraph(details, row.limitations, "muted");
    evidence.append(details);
    paragraph(evidence, `Checked ${date(row.checked_at)}`, "muted");
    tr.append(repo, classification, evidence);
    $("checks").append(tr);
  }
  $("check-count").textContent = `Showing ${Math.min(visible, rows.length)} of ${rows.length} matching assessments (${all.length} total). Each row is one repository / incident / component.`;
  $("more").hidden = visible >= rows.length;
  if (!rows.length) {
    const row = node("tr");
    const cell = node("td", all.length
      ? "No evidence matches the selected repository and text filter."
      : state.latest.incidents.length ? "No scoped repository checks available. See coverage gaps." : "No unresolved incident: incident-time probes are idle. Continuous selected-repository observations, inventory and status polling remain active.");
    cell.colSpan = 3;
    row.append(cell);
    $("checks").append(row);
  }
}
function renderObservations() {
  const panel = $("observations");
  panel.replaceChildren();
  const budget = state.rate_budget;
  if (budget) paragraph(panel, `Shared GitHub request budget: ${state.config.github_requests_hour}/hour, ${state.config.github_requests_minute}/minute. ${budget.attempts_last_hour} attempts this hour; provider remaining estimate ${budget.provider_remaining_estimate ?? "not reported"}. ${budget.deferred_seconds ? `DEFERRED for ${budget.deferred_seconds}s (${budget.reason}).` : "No current rate-budget deferral."} Other clients can consume the same credential quota; limits cannot be guaranteed externally.`, budget.deferred_seconds ? "warning" : "muted");
  const report = state.latest?.observations;
  if (!report?.enabled) {
    paragraph(panel, report?.error || report?.reason || "Continuous observations pending.", report?.error ? "error" : "muted");
    return;
  }
  const rows = report.rows;
  const fresh = row => row.fresh && row.sample && Date.now() / 1000 - row.sample.at >= 0
    && Date.now() / 1000 - row.sample.at <= report.fresh_seconds;
  const observed = rows.filter(row => fresh(row) && !row.gap).length;
  paragraph(panel, `${observed}/${report.selected} selected repositories have fresh Actions run-list observations; ${report.selected - observed} have missing, failed or stale coverage. Freshness target ${report.fresh_seconds / 60} minutes; at most ${report.batch_size} due repositories per cycle. Budgets may prevent meeting this target.`);
  paragraph(panel, `${report.persistent ? "Persisted locally" : "Memory-only demo/test observations"}: up to ${report.retention_days} days / ${report.record_limit} samples across scopes. Baselines require 3 prior operational-Status samples within 24 hours before the incident (or now). A status-operational baseline is not verified healthy behavior.`, "muted");
  paragraph(panel, report.limitation, "muted");
  for (const row of rows) {
    const details = node("details");
    const sample = row.sample;
    const gap = row.gap || (!fresh(row) ? "stale" : null);
    details.append(node("summary", `${row.repository}: ${gap || "run-list observed"} | ${gap ? "observation_unavailable" : row.signal}`));
    paragraph(details, `Last attempt ${sample ? date(new Date(sample.at * 1000).toISOString()) : "never"}; outcome ${sample?.outcome || "none"}. Metadata available: ${row.metadata_available ? "yes (cached validation)" : "no"}. Baseline samples ${row.baseline_samples}; median queued runs ${row.baseline_median_queued_runs ?? "insufficient"}.`);
    if (sample?.outcome === "observed") paragraph(details, `Sample: ${sample.sampled_runs} runs, ${sample.queued_runs} queued/waiting, oldest queued creation age ${sample.oldest_queued_age_seconds ?? "unknown"}s; ${sample.failed_runs}/${sample.completed_runs} completed sampled runs failed/timed out. ${sample.sample_partial ? "Partial run history." : "Returned run-list sample only."} This is not a failure rate or a functional success test.`);
    paragraph(details, `${row.incident_overlap && fresh(row) ? "Sample collected during a mapped Actions incident." : "No fresh mapped Actions incident overlap established."} Queue increases and run failures have multiple possible causes. Other feature functionality remains unverified.`, "muted");
    for (const previous of row.recent_incident_samples) paragraph(details, `Retained incident sample: ${date(new Date(previous.at * 1000).toISOString())}; ${previous.outcome}; incident IDs ${previous.incident_ids.join(", ")}.`, "muted");
    panel.append(details);
  }
}
function render() {
  renderSelection();
  renderCopilotProbe();
  renderObservations();
  const report = state.latest;
  const discovery = state.discovery;
  const known = discovery?.latest || report?.inventory;
  $("refresh").disabled = state.busy || state.refresh_pending || state.stopped;
  $("refresh").textContent = state.busy ? "Assessment running..." : "Refresh assessment";
  const age = report ? Math.max(0, (Date.now() - new Date(report.completed_at)) / 1000) : 0;
  const stale = Boolean(report && age > state.config.poll_seconds + state.config.cycle_seconds);
  const warnings = [];
  if (state.demo) warnings.push("FIXTURE DEMO - synthetic evidence; no live monitoring.");
  if (state.error) warnings.push(`${state.error.message} (${state.error.kind}, ${date(state.error.at)})`);
  if (discovery?.error) warnings.push(`${discovery.error.message} (${discovery.error.kind})`);
  if (stale) warnings.push("STALE snapshot: do not treat as current evidence.");
  if (report && !report.status_available) warnings.push("GitHub Status unavailable; incident state is unknown.");
  if (state.stopped) warnings.push("Monitoring stopped.");
  if (actionMessage) warnings.push(actionMessage);
  const statusText = report ? report.status_description : "Reading GitHub Status and the configured repository scope...";
  $("notice").textContent = [...warnings, statusText].join(" ");
  $("notice").className = warnings.length ? "warning" : "";
  if (!report) {
    renderChecks();
    for (const id of ["summary", "incidents", "agent", "components", "coverage", "checks", "inventory", "history"]) {
      $(id).replaceChildren();
    }
    $("inventory-title").textContent = "Inventory pending";
    $("check-count").textContent = "";
    $("more").hidden = true;
    return;
  }
  $("summary").replaceChildren();
  for (const [label, value] of [
    ["Unresolved incidents", report.incidents.length],
    ["Suspected assessments", report.counts.suspected],
    ["Unknown assessments", report.counts.unknown],
    ["Repositories in scope", known.discovered],
  ]) {
    const metric = node("div", undefined, "metric");
    metric.append(node("strong", value), node("span", label));
    $("summary").append(metric);
  }
  $("incidents").replaceChildren();
  for (const incident of report.incidents) {
    const card = node("article", undefined, "incident");
    card.append(link(incident.name, incident.url));
    paragraph(card, `${incident.status} | Started ${date(incident.started_at)} | Updated ${date(incident.updated_at)}`, "muted");
    paragraph(card, `Affected components: ${incident.components.map((id) => report.components.find((part) => part.id === id)?.name || id).join(", ") || "unscoped - unknown"}`);
    $("incidents").append(card);
  }
  if (!report.incidents.length) paragraph($("incidents"), report.status_available ? "No unresolved incidents reported by GitHub Status. This is not a tenant health guarantee." : "Cannot determine current incidents.", "muted");
  $("agent").replaceChildren();
  const agent = report.agent;
  if (agent) {
    paragraph($("agent"), `Planner: ${agent.status}. ${agent.note}`);
    if (agent.error) paragraph($("agent"), `Planner error/limit: ${agent.error}. Deterministic check-only fallback; not AI reasoning.`, "error");
    if (agent.status === "disabled") paragraph($("agent"), "AI is not enabled (and is always disabled in demo mode). These results use deterministic checks only.", "muted");
    if (agent.status === "idle") paragraph($("agent"), "No eligible mapped incident probe in the bounded planner scope. No model called.", "muted");
    paragraph($("agent"), `Model calls ${agent.model_calls}/${agent.limits.model_calls}; selected reads ${agent.tool_calls}/${agent.limits.tool_calls}; at most ${agent.contexts_considered} assessment contexts considered. Remaining repositories/components still receive deterministic checks.`, "muted");
    if (agent.status === "fallback" && agent.trace.length) paragraph($("agent"), "Validated AI selections before fallback (not a completed AI assessment):", "muted");
    for (const step of agent.trace) {
      const details = node("details");
      details.append(node("summary", `${step.repository || "Planner"} / ${step.component || "stop"}: ${step.operation}`));
      paragraph(details, `Selection rationale: ${step.reason}`);
      paragraph(details, `Uncertainty: ${step.uncertainty}`, "muted");
      paragraph(details, `Sanitized observation: ${JSON.stringify(step.result)}. See repository evidence for the deterministic interpretation.`);
      $("agent").append(details);
    }
  }
  $("components").replaceChildren();
  for (const part of report.components) {
    const card = node("article", undefined, "component");
    card.append(node("h3", part.name), badge(part.status));
    paragraph(card, `Adapter: ${part.adapter}`, "muted");
    paragraph(card, part.limitations);
    $("components").append(card);
  }
  $("coverage").replaceChildren();
  const explicitScope = known.selected_count !== undefined;
  const completedScope = explicitScope ? "selected repository validation complete" : "traversal complete for credential-visible scope";
  paragraph($("coverage"), `Background ${explicitScope ? "selected validation" : "discovery"}: ${known.complete ? completedScope : "IN PROGRESS / PARTIAL / PAUSED"}. ${known.discovered} known. ${discovery?.busy ? "Batch running." : `Next batch ${date(discovery?.next_batch_at)}.`} Discovery/validation continues independently of the assessment poll interval.`);
  paragraph($("coverage"), `${known.next_repository ? "Selected repository validation is checkpointed." : known.next_page ? `Next discovery page: ${known.next_page}; saved progress resumes automatically in a background batch.` : "Discovery/validation pass reached its end; complete selected metadata is revalidated no more often than hourly."} Current pass: ${known.scan_discovered} discovered, started ${date(known.scan_started_at)}. Last unrestricted pass completed: ${date(known.last_completed_at)}. ${known.cached_repositories} cached entries awaiting revalidation.`, "muted");
  if (known.next_repository) paragraph($("coverage"), `Next selected repository: ${known.next_repository}/${known.selected_count}. Validation resumes within the configured discovery budgets.`, "muted");
  for (const error of known.repository_errors || []) paragraph($("coverage"), `${error.repository} / ${error.kind}: ${error.message}`, "error");
  paragraph($("coverage"), known.scope);
  paragraph($("coverage"), known.access_caveat, "error");
  if (known.reason) {
    const checkpoint = ["inventory_deadline", "inventory_request_limit", "deadline", "request_limit"].includes(known.reason.kind);
    paragraph($("coverage"), `${checkpoint ? "Checkpoint saved" : known.reason.kind}: ${known.reason.message}`, checkpoint || known.reason.kind === "scope_paused" ? "muted" : "error");
  }
  paragraph($("coverage"), `Last assessment used ${report.inventory.discovered} repositories known at that time: ${report.coverage.repositories_selected} selected; ${report.coverage.repositories_deferred} repositories / ${report.coverage.assessments_deferred} assessments deferred; ${report.coverage.check_errors} probe errors. Newly discovered repositories enter subsequent assessments, not this historical snapshot.`);
  for (const error of report.errors) paragraph($("coverage"), `${error.source} / ${error.kind}: ${error.message}`, "error");
  paragraph($("coverage"), `${report.coverage.complete ? "Bounded cycle complete" : "PARTIAL / limited coverage"}. ${report.coverage.note}`);
  paragraph($("coverage"), `Snapshot ${date(report.completed_at)} (${Math.floor(age)}s ago); cycle started ${date(report.started_at)}. Status feed updated ${date(report.status_updated_at)}. Next poll ${state.busy ? "after current assessment" : date(state.next_poll_at)}.`);
  paragraph($("coverage"), `Authentication: ${state.authentication}. Requests used ${report.requests_used}/${state.config.max_requests}; interval ${state.config.poll_seconds}s; cycle budget ${state.config.cycle_seconds}s; concurrency ${state.config.workers}. No adapter currently emits confirmed impact; no "unaffected" classification.`, "muted");
  paragraph($("coverage"), `Discovery batch limits: ${state.config.discovery_seconds}s / ${Math.min(state.config.max_requests, state.config.discovery_requests)} attempts; continuation delay ${state.config.discovery_resume_seconds}s; latest batch updated ${date(known.updated_at)}. Background discovery does not repeat incident probes or extend assessment history.`, "muted");
  $("inventory-title").textContent = `${known.discovered} repositories (${known.complete ? completedScope : "resumable discovery/validation / partial or paused inventory"})`;
  $("inventory").replaceChildren(...known.repositories.map((repo) => link(repo.name + (repo.archived ? " (archived)" : ""), repo.url)));
  $("history").replaceChildren();
  for (const item of state.history) {
    paragraph($("history"), `${date(item.completed_at)} | ${item.incidents} incidents | ${item.discovered} repositories | ${item.counts.suspected} suspected, ${item.counts.unknown} unknown | ${item.coverage_complete ? "bounded cycle complete" : "partial"}`, "history-row");
  }
  renderChecks();
}
async function load() {
  if (loading) return;
  loading = true;
  try {
    const response = await fetch("/api/state", {cache: "no-store", signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(`Local server HTTP ${response.status}`);
    state = await response.json();
    render();
  } catch {
    $("notice").textContent = "Local monitor unreachable. Any evidence below is stale; check the server process.";
    $("notice").className = "warning";
    $("refresh").disabled = true;
  } finally { loading = false; }
}
$("refresh").addEventListener("click", async () => {
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/refresh", {method: "POST", signal: AbortSignal.timeout(10000)});
    const data = await response.json();
    actionMessage = response.ok ? "Read-only assessment requested." : `Refresh not started: ${data.status || data.error || "local server error"}.`;
  } catch { actionMessage = "Refresh failed: local server unreachable. No assessment confirmed."; }
  await load();
});
$("copilot-probe").addEventListener("click", async () => {
  if (copilotSubmitting) return;
  copilotSubmitting = true;
  renderCopilotProbe();
  try {
    const response = await fetch("/api/copilot-probe", {method: "POST", signal: AbortSignal.timeout(10000)});
    const data = await response.json();
    copilotMessage = response.ok ? "One active inference requested; this may consume Copilot credits."
      : `Check not started: ${data.status || data.error || "local server error"}.`;
  } catch { copilotMessage = "Request not confirmed. No automatic retry; check the latest sample before trying again."; }
  finally { copilotSubmitting = false; }
  await load();
});
$("filter").addEventListener("input", () => { visible = 50; renderChecks(); });
$("evidence-repository").addEventListener("change", () => { visible = 50; renderChecks(); });
$("more").addEventListener("click", () => { visible += 50; renderChecks(); });
async function saveSelection(mode) {
  const repositories = mode === "selected"
    ? selectedRepositoryNames() : [];
  if (mode === "selected" && (!repositories.length || repositories.length > (state?.selection.limit || 200))) {
    selectionMessage = "Enter at least one repository, within the displayed selection limit.";
    renderSelection();
    return;
  }
  selectionSaving = true;
  selectionMessage = "";
  renderSelection();
  try {
    const response = await fetch("/api/repositories", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode, repositories}), signal: AbortSignal.timeout(10000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Selection not applied: ${data.status || response.status}`);
    selectionDirty = false;
    selectionMessage = data.status === "unchanged"
      ? "This scope is already active or queued; no duplicate discovery or assessment was started."
      : "Scope change accepted. Prior evidence/history is cleared when it takes effect.";
  } catch (error) {
    selectionMessage = error.message || "Repository selection failed; previous scope remains active.";
  } finally {
    selectionSaving = false;
  }
  await load();
}
$("repository-names").addEventListener("input", () => {
  selectionDirty = true;
  renderAssessedRepositoryDropdown();
});
function addPickerRepositories(all) {
  if ($(all ? "select-all-repositories" : "add-selected-repositories").disabled) return;
  const names = new Map(selectedRepositoryNames().map(name => [name.toLowerCase(), name]));
  for (const option of $("assessed-repository").options) {
    if ((all || option.selected) && option.value && !names.has(option.value.toLowerCase())) names.set(option.value.toLowerCase(), option.value);
  }
  const limit = state.selection.limit;
  if (names.size > limit) {
    selectionMessage = `Cannot add ${names.size} repositories: the selection limit is ${limit}. Your draft is unchanged; choose fewer repositories.`;
  } else {
    $("repository-names").value = [...names.values()].join("\n");
    selectionDirty = true;
    selectionMessage = `Draft now contains ${names.size} repositories. Discovery may be partial; newly discovered repositories are not added automatically. Click Save and verify selected repositories to apply.`;
  }
  renderSelection();
}
$("assessed-repository").addEventListener("change", renderAssessedRepositoryDropdown);
$("repository-organization").addEventListener("input", renderAssessedRepositoryDropdown);
$("repository-search").addEventListener("input", renderAssessedRepositoryDropdown);
$("add-selected-repositories").addEventListener("click", () => addPickerRepositories(false));
$("select-all-repositories").addEventListener("click", () => addPickerRepositories(true));
$("repository-selection").addEventListener("submit", (event) => {
  event.preventDefault();
  saveSelection("selected");
});
$("pause-repositories").addEventListener("click", () => saveSelection("paused"));
$("discover-organization").addEventListener("click", async () => {
  if ($("discover-organization").disabled) return;
  selectionSaving = true;
  renderSelection();
  try {
    const response = await fetch("/api/writable-repositories", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({}),
      signal: AbortSignal.timeout(10000),
    });
    const data = await response.json();
    selectionMessage = response.ok ? "Writable repository discovery queued. Assessment scope is unchanged."
      : `Writable discovery not started: ${data.error || data.status || response.status}`;
  } catch { selectionMessage = "Writable discovery request failed; assessment scope is unchanged."; }
  finally { selectionSaving = false; }
  await load();
});
window.addEventListener("hashchange", renderPage);
renderPage();
load();
setInterval(load, 5000);
