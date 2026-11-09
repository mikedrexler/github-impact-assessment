"use strict";
let state = null;
let visible = 50;
let loading = false;
let actionMessage = "";
let selectionDirty = false;
let selectionSaving = false;
let selectionMessage = "";
const $ = (id) => document.getElementById(id);
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (cls) element.className = cls;
  return element;
}
function link(text, url) {
  const element = node("a", text);
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:" && ["github.com", "www.githubstatus.com"].includes(parsed.hostname) && !parsed.username && !parsed.password) {
      element.href = parsed.href;
      element.target = "_blank";
      element.rel = "noreferrer";
    }
  } catch { /* Invalid evidence links remain plain text. */ }
  return element;
}
function date(value) {
  const parsed = new Date(value);
  return value && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleString() : "unknown";
}
function badge(value) {
  const allowed = ["confirmed", "suspected", "unknown", "not-applicable", "operational", "degraded_performance", "partial_outage", "major_outage"];
  return node("span", value.replaceAll("_", " "), `badge ${allowed.includes(value) ? value : "unknown"}`);
}
function paragraph(parent, text, cls) { parent.append(node("p", text, cls)); }
function selectedRepositoryNames() {
  return $("repository-names").value.split(/[\s,]+/).filter(Boolean);
}
function renderAssessedRepositoryDropdown() {
  const dropdown = $("assessed-repository");
  const known = state?.discovery?.latest?.repositories || state?.latest?.inventory?.repositories || [];
  const names = new Map();
  for (const name of [
    ...known.map((repo) => repo.name), ...(state?.selection?.repositories || []),
    ...(state?.selection?.pending?.repositories || []), ...selectedRepositoryNames(),
  ]) {
    names.set(name.toLowerCase(), name);
  }
  const repositories = [...names.values()].sort((a, b) => a.localeCompare(b));
  const values = ["", ...repositories];
  if (JSON.stringify([...dropdown.options].map((option) => option.value)) !== JSON.stringify(values)) {
    dropdown.replaceChildren(...values.map((name) => {
      const option = node("option", name || (repositories.length ? "Choose a repository..." : "Enter an owner/repo below to begin"));
      option.value = name;
      return option;
    }));
    dropdown.value = "";
  }
  dropdown.disabled = !repositories.length || selectionSaving || Boolean(state?.stopped);
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
      ? `Selected scope: ${selection.repositories.length} repositories. No account-wide crawl.`
      : "Account-wide discovery is enabled.";
  $("selection-status").textContent = [
    description,
    selection.pending ? `Scope change queued (${selection.pending.mode}); applies after the running assessment/discovery batch finishes.` : "",
    selectionMessage,
  ].filter(Boolean).join(" ");
  $("selection-help").textContent = `Up to ${selection.limit} exact owner/repo names. Selection is server-memory-only until restart; use --repos owner/repo at startup to preselect before any discovery.`;
  for (const id of ["apply-repositories", "pause-repositories", "all-repositories"]) {
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
      : state.latest.incidents.length ? "No scoped repository checks available. See coverage gaps." : "No unresolved incident: incident-time probes are idle. Inventory and status polling remain active.");
    cell.colSpan = 3;
    row.append(cell);
    $("checks").append(row);
  }
}
function render() {
  renderSelection();
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
  paragraph($("coverage"), `${known.next_repository ? "Selected repository validation is checkpointed." : known.next_page ? `Next discovery page: ${known.next_page}; saved progress resumes automatically in a background batch.` : "Discovery/validation pass reached its end; revalidation waits for the configured poll interval."} Current pass: ${known.scan_discovered} discovered, started ${date(known.scan_started_at)}. Last unrestricted pass completed: ${date(known.last_completed_at)}. ${known.cached_repositories} cached entries awaiting revalidation.`, "muted");
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
$("assessed-repository").addEventListener("change", () => {
  const name = $("assessed-repository").value;
  if (!name) return;
  const names = selectedRepositoryNames();
  if (!names.some((existing) => existing.toLowerCase() === name.toLowerCase())) names.push(name);
  $("repository-names").value = names.join("\n");
  selectionDirty = true;
  $("assessed-repository").value = "";
  selectionMessage = "Repository added to your draft selection. Click Assess selected repositories to apply.";
  renderSelection();
});
$("repository-selection").addEventListener("submit", (event) => {
  event.preventDefault();
  saveSelection("selected");
});
$("pause-repositories").addEventListener("click", () => saveSelection("paused"));
$("all-repositories").addEventListener("click", () => saveSelection("all"));
load();
setInterval(load, 5000);
