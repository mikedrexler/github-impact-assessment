"""Conservative evidence interpretation, never inference from generic connectivity."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from typing import Any

from .client import Client, InventoryState, ProbeError, inventory, repository_path


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
    return {
        "actions": "actions", "git operations": "git", "api requests": "api",
        "webhooks": "webhooks", "pull requests": "pull_requests",
        "issues": "issues", "pages": "pages", "packages": "packages",
        "codespaces": "codespaces", "copilot": "copilot",
        "copilot ai model providers": "copilot", "github copilot": "copilot",
    }.get(name.casefold().strip(), "unsupported")


LIMITATIONS = {
    "actions": "Samples the 30 most recently created runs; first 100 jobs and first 100 check runs for at most 5 incident-time runs. No workflow execution, runner probing, logs or exhaustive history.",
    "git": "Reads one commit reference through REST, not git clone/fetch/push. Git transport health cannot be established.",
    "api": "Reads repository metadata. Success proves only this endpoint responded at this instant.",
    "webhooks": "Webhook configuration/delivery reads require elevated access and can expose sensitive payloads; not inspected.",
    "pull_requests": "Reads one open pull request; cannot test creation, merging, reviews or notifications.",
    "issues": "Reads one issue (REST can include pull requests); cannot test creation, edits or notifications.",
    "pages": "Reads Pages configuration only. No deployment, build, DNS or website availability test.",
    "packages": "No uniform repository-scoped read-only probe establishes package pull/push health.",
    "codespaces": "No codespace lifecycle operation. Repository metadata cannot establish Codespaces health.",
    "copilot": "No meaningful tenant-level read-only inference/IDE/model-provider probe; licensing or API connectivity would not establish health.",
    "unsupported": "Unrecognized/informational live Status component: no supported read-only evidence adapter.",
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


def actions(client: Client, repo: dict, incident: dict, row: dict, now: datetime):
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


def probe(client: Client, repo: dict, component: dict, incident: dict, now: datetime) -> dict:
    adapter = adapter_for(component["name"])
    row = {
        "repository": repo["name"], "repository_url": repo["url"],
        "component_id": component["id"], "component": component["name"],
        "incident_id": incident["id"], "incident": incident.get("name", "Unnamed incident"),
        "classification": "unknown", "reason": LIMITATIONS[adapter],
        "evidence": [], "limitations": LIMITATIONS[adapter],
        "checked_at": None, "adapter": adapter, "error_kind": None,
    }
    if adapter in {"unsupported", "webhooks", "packages", "codespaces", "copilot"}:
        row["checked_at"] = timestamp()
        return row
    if (adapter == "issues" and repo["has_issues"] is False) or (adapter == "pages" and repo["has_pages"] is False):
        row.update(classification="not-applicable", reason=f"Repository reports {'Issues disabled' if adapter == 'issues' else 'no configured Pages site'}", checked_at=timestamp())
        return row
    path = repository_path(repo["name"])
    endpoints = {
        "git": ("/commits?per_page=1&page=1", "/commits", "Commit metadata"),
        "api": ("", "", "Repository metadata"),
        "pull_requests": ("/pulls?state=open&per_page=1&page=1", "/pulls", "Open pull request sample"),
        "issues": ("/issues?state=all&per_page=1&page=1", "/issues", "Issue sample"),
        "pages": ("/pages", "/settings/pages", "Pages configuration"),
    }
    try:
        if adapter == "actions":
            actions(client, repo, incident, row, now)
        else:
            endpoint, suffix, description = endpoints[adapter]
            response = client.get(path + endpoint)
            if adapter in {"git", "pull_requests", "issues"}:
                list_data(response)
            else:
                object_data(response)
            row["evidence"].append({
                "summary": f"{description} read succeeded; does not establish component health",
                "url": repo["url"] + suffix,
            })
            row["reason"] = "Read-only sample succeeded, but cannot prove presence or absence of incident impact"
    except ProbeError as error:
        row["error_kind"] = error.kind
        row["reason"] = error.message + "; impact is unknown"
        row["evidence"].append({
            "summary": f"Read-only probe error: {error.kind}. Permissions, local connectivity and endpoint errors are not proof of impact.",
            "url": repo["url"],
        })
    row["checked_at"] = timestamp()
    return row


def assess(client: Client, offset: int = 0, inventory_state: InventoryState | None = None) -> dict:
    started = timestamp()
    errors = []
    try:
        status = client.status()
        status_available = True
    except ProbeError as error:
        status = {"components": [], "incidents": []}
        status_available = False
        errors.append({"source": "status", "kind": error.kind, "message": error.message})
    found = inventory(
        client, deadline=time.monotonic() + client.config.cycle_seconds / 2,
        request_limit=client.config.max_requests // 2 if client.config.max_requests >= 4 else None,
        state=inventory_state,
    )
    components = status["components"]
    incidents = [item for item in status["incidents"] if item.get("status") not in {"resolved", "postmortem"}]
    mapped = []
    for incident in incidents:
        affected = {item.get("id") for item in incident.get("components", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        matches = [component for component in components if component["id"] in affected]
        if not matches or affected - {component["id"] for component in matches}:
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

    def check(task: tuple) -> dict:
        repo, component, incident = task
        return probe(client, repo, component, incident, now)

    with ThreadPoolExecutor(max_workers=client.config.workers) as pool:
        rows = list(pool.map(check, tasks))
    for warning in sorted(getattr(client, "access_warnings", set())):
        errors.append({"source": "github", "kind": "sso_partial_results", "message": warning})
    counts = {key: sum(row["classification"] == key for row in rows)
              for key in ("confirmed", "suspected", "unknown", "not-applicable")}
    issue_rows = sum(row["error_kind"] is not None for row in rows)
    return {
        "started_at": started, "completed_at": timestamp(),
        "status_available": status_available,
        "status_updated_at": object_data(status.get("page", {})).get("updated_at"),
        "status_description": object_data(status.get("status", {})).get("description", "Status unavailable"),
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
            "components": [part.get("id") for part in item.get("components", []) if isinstance(part, dict)],
        } for item in incidents],
        "inventory": found, "checks": rows, "counts": counts, "errors": errors,
        "coverage": {
            "repositories_selected": selected_count,
            "repositories_deferred": len(repos) - selected_count if mapped else 0,
            "assessments_deferred": len(repos) * len(mapped) - len(rows),
            "check_errors": issue_rows, "mapped_incident_components": len(mapped),
            "complete": found["complete"] and status_available and not errors and issue_rows == 0
                        and len(rows) == len(repos) * len(mapped),
            "note": "Completion means bounded enumeration/probes finished, never proof of service health or full account access. No incident-time probes without a mapped unresolved incident.",
        },
        "requests_used": client.requests,
        "next_offset": (offset + selected_count) % len(repos) if repos else 0,
    }
