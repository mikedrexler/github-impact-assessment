"""Conservative evidence interpretation, never inference from generic connectivity."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from typing import TYPE_CHECKING, Any, Protocol

from .client import Client, InventoryState, ProbeError, inventory, repository_path

if TYPE_CHECKING:
    from .agent import Agent
    from .observations import Observations


class ReadClient(Protocol):
    def get(self, path: str) -> Any: ...


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def parsed(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def adapter_for(name: str) -> str:
    """Live IDs map incidents; names only select a conservative evidence adapter."""
    return "actions" if name.casefold().strip() == "actions" else "unsupported"


LIMITATIONS = {
    "actions": "Samples the 30 most recently created runs; first 100 jobs and first 100 check runs for at most 5 incident-time runs. No workflow execution, runner probing, logs or exhaustive history.",
    "unsupported": "Outside the Actions-only assessment scope; no probe is performed.",
}


def incident_start(incident: dict) -> datetime | None:
    return parsed(incident.get("started_at")) or parsed(incident.get("created_at"))


def relevant(run: dict, incident: dict, now: datetime) -> bool:
    start = incident_start(incident)
    if start is None:
        return False
    created = parsed(run.get("created_at"))
    updated = parsed(run.get("updated_at"))
    if not created or created > now:
        return False
    if run.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}:
        return True
    return updated is not None and start <= updated <= now


def safe_github_url(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.startswith("https://github.com/"):
        return value
    return fallback


def object_data(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ProbeError("invalid_response", "Endpoint returned an unexpected object")
    return value


def list_data(value: Any) -> list:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProbeError("invalid_response", "Endpoint returned an unexpected list")
    return value


def validate_states(rows: list):
    for row in rows:
        for field in ("status", "conclusion"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ProbeError("invalid_response", "Endpoint returned an invalid status/conclusion")


def actions(client: ReadClient, repo: dict, incident: dict, row: dict, now: datetime):
    path = repository_path(repo["name"])
    data = object_data(client.get(path + "/actions/runs?per_page=30&page=1"))
    runs = list_data(data.get("workflow_runs"))
    validate_states(runs)
    total = data.get("total_count")
    row["evidence"].append({
        "summary": f"Sampled {len(runs)} recent runs; total reported: {total if isinstance(total, int) else 'unknown'}",
        "url": repo["url"] + "/actions",
    })
    candidates = [run for run in runs if relevant(run, incident, now)]
    if not candidates:
        row["reason"] = "No incident-overlapping runs in sample; absent/historical evidence cannot establish impact"
        return
    suspected = False
    for run in candidates[:5]:
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
            raise ProbeError("invalid_response", "Workflow run lacks a valid numeric ID")
        url = safe_github_url(run.get("html_url"), repo["url"] + "/actions")
        conclusion = run.get("conclusion")
        row["evidence"].append({
            "summary": f"Run {run_id}: {run.get('status', 'unknown')}, conclusion {conclusion or 'none'}; updated {run.get('updated_at', 'unknown')}",
            "url": url,
        })
        job_data = object_data(client.get(path + f"/actions/runs/{run_id}/jobs?per_page=100&page=1"))
        jobs = list_data(job_data.get("jobs"))
        validate_states(jobs)
        job_total = job_data.get("total_count")
        if not isinstance(job_total, int) or job_total > len(jobs):
            row["evidence"].append({"summary": "Job evidence partial/unknown (first 100 jobs only)", "url": url})
        if conclusion in {"failure", "timed_out", "startup_failure"}:
            row["evidence"].append({
                "summary": "Failed run is not attributed to GitHub: application, configuration and runner failures remain possible",
                "url": url,
            })
        for job in jobs:
            labels = job.get("labels", [])
            labels = labels if isinstance(labels, list) else []
            self_hosted = any(str(label).casefold() == "self-hosted" for label in labels)
            created = parsed(job.get("created_at"))
            start = incident_start(incident)
            is_queued = job.get("status") in {"queued", "waiting", "pending", "requested"}
            age = (now - created).total_seconds() if created else 0
            if is_queued and created and start and start <= created <= now and age >= 600:
                row["evidence"].append({
                    "summary": f"Job {job.get('id', '?')} currently queued/waiting, created {int(age // 60)} minutes ago during incident. "
                    + ("Self-hosted runner capacity/offline state is a confounder; no platform attribution."
                       if self_hosted else
                       "Runner type/capacity, concurrency controls and approvals remain confounders; creation age is not continuous queue duration."),
                    "url": safe_github_url(job.get("html_url"), url),
                })
                if not self_hosted:
                    suspected = True
        sha = run.get("head_sha")
        if isinstance(sha, str) and re.fullmatch(r"[a-fA-F0-9]{40,64}", sha):
            checks = object_data(client.get(path + f"/commits/{sha}/check-runs?per_page=100&page=1"))
            sampled = list_data(checks.get("check_runs"))
            validate_states(sampled)
            failures = sum(check.get("conclusion") in {"failure", "timed_out", "startup_failure"} for check in sampled)
            row["evidence"].append({
                "summary": f"Sampled {len(sampled)} commit check runs (first page only); {failures} failures/timeouts. Includes external apps; neither failures nor successes establish platform health.",
                "url": repo["url"] + f"/commit/{sha}/checks",
            })
    row["classification"] = "suspected" if suspected else "unknown"
    row["reason"] = (
        "Incident-time queue symptom observed; correlation only, not confirmed platform impact"
        if suspected else "Workflow/job/check evidence sampled; no attributable platform failure established"
    )


def probe(client: ReadClient, repo: dict, component: dict, incident: dict, now: datetime) -> dict:
    adapter = adapter_for(component["name"])
    row = {
        "repository": repo["name"], "repository_url": repo["url"],
        "component_id": component["id"], "component": component["name"],
        "incident_id": incident["id"], "incident": incident.get("name", "Unnamed incident"),
        "classification": "unknown", "reason": LIMITATIONS[adapter],
        "evidence": [], "limitations": LIMITATIONS[adapter],
        "checked_at": None, "adapter": adapter, "error_kind": None,
    }
    if adapter != "actions":
        row["checked_at"] = timestamp()
        return row
    try:
        actions(client, repo, incident, row, now)
    except ProbeError as error:
        row["error_kind"] = error.kind
        row["reason"] = error.message + "; impact is unknown"
        row["evidence"].append({
            "summary": f"Read-only probe error: {error.kind}. Permissions, local connectivity and endpoint errors are not proof of impact.",
            "url": repo["url"],
        })
    row["checked_at"] = timestamp()
    return row


def assess(
    client: Client, offset: int = 0, inventory_state: InventoryState | None = None,
    *, inventory_report: dict | None = None, agent: Agent | None = None,
    observations: Observations | None = None, observation_scope: str = "",
    selected_names: tuple[str, ...] | None = None,
) -> dict:
    from .agent import MAX_CONTEXTS, Agent, ProbeContext

    started = timestamp()
    errors = []
    try:
        status = client.status()
        status_available = True
    except ProbeError as error:
        status = {"components": [], "incidents": []}
        status_available = False
        errors.append({"source": "status", "kind": error.kind, "message": error.message})
    found = inventory_report if inventory_report is not None else inventory(
        client, deadline=time.monotonic() + client.config.cycle_seconds / 2,
        request_limit=client.config.max_requests // 2 if client.config.max_requests >= 4 else None,
        state=inventory_state,
    )
    all_components = status["components"]
    components = [part for part in all_components if adapter_for(part["name"]) == "actions"]
    action_ids = {part["id"] for part in components}
    if status_available and not components:
        errors.append({"source": "status", "kind": "actions_component_missing",
                       "message": "GitHub Status did not expose an Actions component; Actions coverage is unknown."})
    incidents = [item for item in status["incidents"] if item.get("status") not in {"resolved", "postmortem"}
                 and any(isinstance(part, dict) and part.get("id") in action_ids for part in item.get("components", []))]
    mapped = []
    for incident in incidents:
        affected = {item.get("id") for item in incident.get("components", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        matches = [component for component in components if component["id"] in affected]
        if affected - {component["id"] for component in all_components}:
            errors.append({
                "source": "mapping", "kind": "unscoped_incident",
                "message": f"Incident {incident['id']} has missing/unrecognized component IDs; scope is not inferred from its title",
            })
        mapped.extend((component, incident) for component in matches)
    active_ids = {component["id"] for component, _ in mapped}
    for component in components:
        if component.get("status") != "operational" and component["id"] not in active_ids:
            errors.append({
                "source": "status", "kind": "component_without_incident",
                "message": f"{component['name']} reports {component.get('status', 'unknown')} without a mapped unresolved incident; no incident invented",
            })
    repos = found["repositories"]
    if mapped and repos:
        offset %= len(repos)
        rotated = repos[offset:] + repos[:offset]
        selected = rotated[:client.config.max_check_repositories]
    else:
        selected = []
    now = datetime.now(timezone.utc)

    tasks = list(islice(
        ((repo, component, incident) for repo in selected for component, incident in mapped),
        client.config.max_assessments,
    ))
    selected_count = len({repo["name"] for repo, _, _ in tasks})

    now = datetime.now(timezone.utc)
    contexts = [ProbeContext(client, repo, component, incident, now)
                for repo, component, incident in tasks]
    agent_report = (agent or Agent()).run(contexts[:MAX_CONTEXTS], getattr(client, "stop", threading.Event()))

    def check(indexed_task: tuple) -> dict:
        index, task = indexed_task
        repo, component, incident = task
        reader = contexts[index] if index < len(contexts) else client
        return probe(reader, repo, component, incident, now)

    with ThreadPoolExecutor(max_workers=client.config.workers) as pool:
        rows = list(pool.map(check, enumerate(tasks)))
    observation_report = {"enabled": False, "reason": "Continuous observations require an explicit selected repository scope."}
    if observations and selected_names:
        import sqlite3
        import sys

        try:
            cached = {path: value for context in contexts for path, value in context.cache.items()}
            observation_report = observations.collect(
                client, observation_scope, selected_names, repos, status_available, components, incidents, cached,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError) as error:
            observation_report = {
                "enabled": False, "error": "Observation storage/processing failed; continuous coverage is unavailable. Prior evidence is not current.",
            }
            print(f"Observation recording failed ({type(error).__name__}); no raw response logged", file=sys.stderr)
    for warning in sorted(getattr(client, "access_warnings", set())):
        errors.append({"source": "github", "kind": "sso_partial_results", "message": warning})
    counts = {key: sum(row["classification"] == key for row in rows)
              for key in ("confirmed", "suspected", "unknown", "not-applicable")}
    issue_rows = sum(row["error_kind"] is not None for row in rows)
    resource_errors = {"rate_limit", "request_limit", "deadline", "cancelled"}
    deferred = {row["repository"] for row in rows if row["error_kind"] in resource_errors}
    stride = selected_count
    if deferred:
        stride = max(1, next(index for index, repo in enumerate(selected) if repo["name"] in deferred))
    return {
        "started_at": started, "completed_at": timestamp(),
        "status_available": status_available,
        "status_updated_at": object_data(status.get("page", {})).get("updated_at"),
        "status_description": ("Actions: " + ", ".join(sorted({part.get("status", "unknown") for part in components})))
                              if components else "Actions status unavailable",
        "components": [{
            "id": component["id"], "name": component["name"],
            "status": component.get("status", "unknown"),
            "adapter": adapter_for(component["name"]),
            "limitations": LIMITATIONS[adapter_for(component["name"])],
        } for component in components],
        "incidents": [{
            "id": item["id"], "name": item.get("name", "Unnamed incident"),
            "status": item.get("status", "unknown"), "started_at": item.get("started_at") or item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "url": "https://www.githubstatus.com/incidents/" + item["id"],
            "components": [part.get("id") for part in item.get("components", []) if isinstance(part, dict) and part.get("id") in action_ids],
        } for item in incidents],
        "inventory": found, "checks": rows, "counts": counts, "errors": errors, "agent": agent_report,
        "observations": observation_report,
        "coverage": {
            "repositories_selected": selected_count,
            "repositories_deferred": len(repos) - selected_count if mapped else 0,
            "assessments_deferred": len(repos) * len(mapped) - len(rows),
            "check_errors": issue_rows, "mapped_incident_components": len(mapped),
            "rate_or_budget_deferred_repositories": len(deferred),
            "continuous_observations_fresh": (
                bool(observation_report.get("enabled") and observation_report.get("gaps") == 0)
                if selected_names else None
            ),
            "complete": found["complete"] and status_available and not errors and issue_rows == 0
                        and len(rows) == len(repos) * len(mapped),
            "note": "Completion means bounded enumeration/probes finished, never proof of service health or full account access. No incident-time probes without a mapped unresolved incident.",
        },
        "requests_used": client.requests,
        "next_offset": (offset + stride) % len(repos) if repos else 0,
    }
