"use strict";
let state = null;
let visible = 50;
let loading = false;
let actionMessage = "";
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
function renderChecks() {
  if (!state?.latest) return;
  const query = $("filter").value.trim().toLowerCase();
  const all = state.latest.checks;
  const rows = all.filter((row) => `${row.repository} ${row.component} ${row.classification} ${row.reason}`.toLowerCase().includes(query));
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
  if (!all.length) {
    const row = node("tr");
    const cell = node("td", state.latest.incidents.length ? "No scoped repository checks available. See coverage gaps." : "No unresolved incident: incident-time probes are idle. Inventory and status polling remain active.");
    cell.colSpan = 3;
    row.append(cell);
    $("checks").append(row);
  }
}
function render() {
  const report = state.latest;
  $("refresh").disabled = state.busy || state.refresh_pending || state.stopped;
  $("refresh").textContent = state.busy ? "Assessment running..." : "Refresh assessment";
  const age = report ? Math.max(0, (Date.now() - new Date(report.completed_at)) / 1000) : 0;
  const stale = Boolean(report && age > state.config.poll_seconds + state.config.cycle_seconds);
  const warnings = [];
  if (state.demo) warnings.push("FIXTURE DEMO - synthetic evidence; no live monitoring.");
  if (state.error) warnings.push(`${state.error.message} (${state.error.kind}, ${date(state.error.at)})`);
  if (stale) warnings.push("STALE snapshot: do not treat as current evidence.");
  if (report && !report.status_available) warnings.push("GitHub Status unavailable; incident state is unknown.");
  if (state.stopped) warnings.push("Monitoring stopped.");
  if (actionMessage) warnings.push(actionMessage);
  const statusText = report ? report.status_description : "First assessment in progress; larger inventories may take up to the configured cycle budget.";
  $("notice").textContent = [...warnings, statusText].join(" ");
  $("notice").className = warnings.length ? "warning" : "";
  if (!report) {
    for (const id of ["summary", "incidents", "components", "coverage", "checks", "inventory", "history"]) {
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
    ["Repositories discovered", report.inventory.discovered],
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
  $("components").replaceChildren();
  for (const part of report.components) {
    const card = node("article", undefined, "component");
    card.append(node("h3", part.name), badge(part.status));
    paragraph(card, `Adapter: ${part.adapter}`, "muted");
    paragraph(card, part.limitations);
    $("components").append(card);
  }
  $("coverage").replaceChildren();
  paragraph($("coverage"), `Repository discovery: ${report.inventory.complete ? "traversal complete for credential-visible scope" : "IN PROGRESS / PARTIAL"}. ${report.inventory.discovered} known; ${report.coverage.repositories_selected} selected for this incident cycle; ${report.coverage.repositories_deferred} repositories / ${report.coverage.assessments_deferred} assessments deferred; ${report.coverage.check_errors} probe errors.`);
  paragraph($("coverage"), `${report.inventory.next_page ? `Next discovery page: ${report.inventory.next_page}; progress resumes automatically next cycle.` : "Discovery pass reached its end; next cycle revalidates from page 1."} Current pass: ${report.inventory.scan_discovered} discovered, started ${date(report.inventory.scan_started_at)}. Last unrestricted pass completed: ${date(report.inventory.last_completed_at)}. ${report.inventory.cached_repositories} cached entries awaiting revalidation.`, "muted");
  paragraph($("coverage"), report.inventory.scope);
  paragraph($("coverage"), report.inventory.access_caveat, "error");
  if (report.inventory.reason) paragraph($("coverage"), `${report.inventory.reason.kind}: ${report.inventory.reason.message}`, "error");
  for (const error of report.errors) paragraph($("coverage"), `${error.source} / ${error.kind}: ${error.message}`, "error");
  paragraph($("coverage"), `${report.coverage.complete ? "Bounded cycle complete" : "PARTIAL / limited coverage"}. ${report.coverage.note}`);
  paragraph($("coverage"), `Snapshot ${date(report.completed_at)} (${Math.floor(age)}s ago); cycle started ${date(report.started_at)}. Status feed updated ${date(report.status_updated_at)}. Next poll ${state.busy ? "after current assessment" : date(state.next_poll_at)}.`);
  paragraph($("coverage"), `Authentication: ${state.authentication}. Requests used ${report.requests_used}/${state.config.max_requests}; interval ${state.config.poll_seconds}s; cycle budget ${state.config.cycle_seconds}s; concurrency ${state.config.workers}. No adapter currently emits confirmed impact; no "unaffected" classification.`, "muted");
  $("inventory-title").textContent = `${report.inventory.discovered} repositories (${report.inventory.complete ? "credential-visible discovery complete" : "resumable discovery / partial inventory"})`;
  $("inventory").replaceChildren(...report.inventory.repositories.map((repo) => link(repo.name + (repo.archived ? " (archived)" : ""), repo.url)));
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
$("more").addEventListener("click", () => { visible += 50; renderChecks(); });
load();
setInterval(load, 5000);
