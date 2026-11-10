"use strict";
let historyState = null;
let historyLoading = false;
let renderedHistory = "";

function renderPast() {
  const query = $("filter").value.trim().toLowerCase();
  const all = historyState.incidents;
  const incidents = all.filter((item) => `${item.name} ${item.components.join(" ")} ${item.started_at} ${date(item.started_at)}`.toLowerCase().includes(query));
  $("scope").textContent = historyState.scope;
  $("count").textContent = `Showing ${incidents.length} of ${all.length} recent resolved Actions incidents. Dates use ${Intl.DateTimeFormat().resolvedOptions().timeZone}.`;
  $("past-incidents").replaceChildren();
  let priorDay = "";
  for (const item of incidents) {
    const start = new Date(item.started_at);
    const day = item.started_at && !Number.isNaN(start.valueOf()) ? start.toLocaleDateString(undefined, {year: "numeric", month: "long", day: "numeric"}) : "Unknown incident date";
    if (day !== priorDay) {
      $("past-incidents").append(node("h2", day, "incident-date"));
      priorDay = day;
    }
    const card = node("article", undefined, "incident");
    const title = node("h3");
    title.append(link(item.name, item.url));
    card.append(title, badge(item.status));
    paragraph(card, `Started ${date(item.started_at)} | Resolved ${date(item.resolved_at)}`);
    paragraph(card, `Affected components: ${item.components.join(", ") || "Not specified"}`, "muted");
    const assessment = item.assessment;
    if (!assessment) {
      paragraph(card, "Environment impact: unknown - no incident-time assessment recorded by this monitor.", "unknown");
      paragraph(card, "The monitor may not have been running, may have had no usable inventory, or the bounded evidence cache may have expired. Historical queue state cannot be reconstructed from current metadata.", "muted");
    } else {
      const observed = node("details");
      observed.append(node("summary", "Recorded environment evidence (latest observed sample)"));
      paragraph(observed, `Observed ${date(assessment.completed_at)}; ${assessment.inventory_discovered} repositories known then. ${assessment.checks.length} repository/component rows: ${Object.entries(assessment.counts).map(([key, count]) => `${count} ${key}`).join(", ")}.`);
      paragraph(observed, `${assessment.coverage.complete ? "Bounded cycle complete" : "PARTIAL coverage"}; ${assessment.coverage.repositories_deferred} repositories and ${assessment.coverage.assessments_deferred} assessment rows deferred across that cycle. These are historical observations, not current health or proof of GitHub causation.`, "muted");
      for (const row of assessment.checks) {
        const detail = node("details", undefined, "history-row");
        detail.append(node("summary", `${row.repository} / ${row.component}: ${row.classification}`));
        paragraph(detail, row.reason);
        if (row.error_kind) paragraph(detail, `Probe error: ${row.error_kind}`, "error");
        for (const evidence of row.evidence) {
          const p = node("p");
          p.append(link(evidence.summary, evidence.url));
          detail.append(p);
        }
        paragraph(detail, row.limitations, "muted");
        paragraph(detail, `Checked ${date(row.checked_at)}`, "muted");
        observed.append(detail);
      }
      card.append(observed);
    }
    const timeline = node("details");
    timeline.append(node("summary", "Public incident timeline"));
    if (!item.updates.length) paragraph(timeline, "No public timeline updates available.", "muted");
    for (const update of item.updates) {
      paragraph(timeline, `${date(update.at)} | ${update.status}`, "muted");
      paragraph(timeline, update.body, "timeline-body");
    }
    if (item.timeline_partial) paragraph(timeline, "Timeline limited to 100 updates.", "error");
    card.append(timeline);
    $("past-incidents").append(card);
  }
  if (!incidents.length) paragraph($("past-incidents"), historyState.available ? "No resolved Actions incidents match this view in the bounded public history response." : "Incident history unavailable. See the refresh error above.", "muted");
}

async function loadPast() {
  if (historyLoading) return;
  historyLoading = true;
  try {
    const response = await fetch("/api/past-incidents", {cache: "no-store", signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error("Local history request failed");
    historyState = await response.json();
    const warnings = [];
    if (historyState.demo) warnings.push("FIXTURE DEMO - all environment evidence is synthetic.");
    if (historyState.error) warnings.push(`${historyState.error.message} (${historyState.error.kind}, ${date(historyState.error.at)}). Prior history may be stale.`);
    const age = historyState.updated_at ? (Date.now() - new Date(historyState.updated_at)) / 1000 : 0;
    if (age > historyState.poll_seconds + historyState.cycle_seconds) warnings.push("STALE public history snapshot.");
    $("notice").textContent = [...warnings, `Public history last refreshed ${date(historyState.updated_at)}. Refresh follows the monitor's assessment schedule.`].join(" ");
    $("notice").className = warnings.length ? "warning" : "";
    const signature = JSON.stringify(historyState);
    if (signature !== renderedHistory) {
      renderPast();
      renderedHistory = signature;
    }
  } catch {
    $("notice").textContent = "Local history unavailable. Any displayed evidence is stale; check the monitor process.";
    $("notice").className = "warning";
  } finally { historyLoading = false; }
}
$("filter").addEventListener("input", () => { if (historyState) renderPast(); });
loadPast();
setInterval(loadPast, 5000);
