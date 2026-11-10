"""Bounded Actions observations, never a service-health or causal verdict."""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import time
from contextlib import contextmanager
from pathlib import Path

from .assessment import list_data, object_data, parsed, validate_states
from .client import ProbeError, repository_path

RETENTION_SECONDS = 14 * 86400
MAX_RECORDS = 20000
FRESH_SECONDS = 900
BATCH_SIZE = 20
LIMITATION = (
    "Actions run-list observation only (30 most recently created runs). Queue age is not continuous "
    "queue duration; approvals, concurrency, runner capacity and application failures are confounders. "
    "No Git transport, writes, workflow execution, delivery, website, package, Codespaces or IDE test. "
    "Missing/empty/successful samples never establish unaffected status or GitHub causation."
)


class ObservationStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._rows: list[dict] = []
        if path is None:
            return
        if path is not None:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            os.close(descriptor)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, repository TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS observation_scope ON observations(scope, repository, at)")
            db.execute("DELETE FROM observations WHERE at<?", (time.time() - RETENTION_SECONDS,))

    @contextmanager
    def _connect(self):
        if self.path is None:
            raise ValueError("Persistent observation path required")
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def records(self, scope: str, now: float) -> list[dict]:
        if self.path is None:
            return [{key: value for key, value in row.items() if key != "scope"}
                    for row in self._rows if row["scope"] == scope and row["at"] >= now - RETENTION_SECONDS]
        with self._connect() as db:
            rows = db.execute(
                "SELECT repository,at,data FROM observations WHERE scope=? AND at>=? ORDER BY at,id LIMIT ?",
                (scope, now - RETENTION_SECONDS, MAX_RECORDS),
            ).fetchall()
        return [{"repository": repo, "at": at, **json.loads(data)} for repo, at, data in rows]

    def append(self, scope: str, repository: str, at: float, data: dict):
        if self.path is None:
            self._rows = [row for row in self._rows if row["at"] >= at - RETENTION_SECONDS]
            self._rows.append({"scope": scope, "repository": repository, "at": at, **data})
            self._rows = self._rows[-MAX_RECORDS:]
            return
        with self._connect() as db:
            db.execute("INSERT INTO observations(scope,repository,at,data) VALUES(?,?,?,?)",
                       (scope, repository, at, json.dumps(data, allow_nan=False)))
            db.execute("DELETE FROM observations WHERE at<?", (at - RETENTION_SECONDS,))
            db.execute("DELETE FROM observations WHERE id NOT IN (SELECT id FROM observations ORDER BY id DESC LIMIT ?)", (MAX_RECORDS,))


def metrics(data: object, now: float) -> dict:
    data = object_data(data)
    runs = list_data(data.get("workflow_runs"))
    if len(runs) > 30:
        raise ProbeError("invalid_response", "Actions sample exceeded requested bound")
    validate_states(runs)
    queued = [run for run in runs if run.get("status") in {"queued", "waiting", "pending", "requested"}]
    ages = []
    for run in queued:
        at = parsed(run.get("created_at"))
        if at and 0 <= now - at.timestamp() <= RETENTION_SECONDS:
            ages.append(round(now - at.timestamp()))
    completed = [run for run in runs if run.get("status") == "completed"]
    failed = sum(run.get("conclusion") in {"failure", "timed_out", "startup_failure"} for run in completed)
    total = data.get("total_count")
    return {
        "sampled_runs": len(runs), "queued_runs": len(queued),
        "oldest_queued_age_seconds": max(ages) if ages else None,
        "completed_runs": len(completed), "failed_runs": failed,
        "sample_partial": not isinstance(total, int) or isinstance(total, bool) or total > len(runs),
    }


class Observations:
    def __init__(self, store: ObservationStore):
        self.store = store

    def collect(self, client, scope: str, names: tuple[str, ...], repositories: list[dict],
                status_available: bool, components: list[dict], incidents: list[dict], cached: dict) -> dict:
        now = time.time()
        records = self.store.records(scope, now)
        latest = {}
        for item in records:
            latest[item["repository"].casefold()] = item
        available = {repo["name"].casefold(): repo for repo in repositories}
        actions_ids = {component["id"] for component in components if component["name"].casefold() == "actions"}
        active = [incident for incident in incidents if actions_ids.intersection(
            item.get("id") for item in incident.get("components", []) if isinstance(item, dict))]
        incident_ids = [incident["id"] for incident in active][:50]
        operational = status_available and bool(actions_ids) and all(
            component.get("status") == "operational" for component in components if component["id"] in actions_ids
        ) and not active
        # Oldest attempts first, including failed attempts, so inaccessible repos
        # cannot monopolize the queue and later selected repos are not starved.
        def reusable(name):
            repo = available.get(name.casefold())
            path = repository_path(repo["name"]) + "/actions/runs?per_page=30&page=1" if repo else ""
            return path in cached and not isinstance(cached[path], ProbeError)

        due = sorted(names, key=lambda name: (not reusable(name), latest.get(name.casefold(), {}).get("at", 0)))
        attempts = 0
        for name in due:
            previous = latest.get(name.casefold())
            if previous and now - previous["at"] < client.config.poll_seconds:
                continue
            if name.casefold() not in available:
                continue
            if attempts >= BATCH_SIZE or client.stop.is_set():
                break
            attempts += 1
            path = repository_path(available[name.casefold()]["name"]) + "/actions/runs?per_page=30&page=1"
            sample = {"outcome": "observed", "incident_ids": incident_ids, "baseline_eligible": operational}
            try:
                value = cached[path] if path in cached else client.get(path)
                if isinstance(value, ProbeError):
                    raise value
                sample.update(metrics(value, now))
                if getattr(client, "access_warnings", set()):
                    sample.update(outcome="sso_partial_results", baseline_eligible=False)
            except ProbeError as error:
                sample.update(outcome=error.kind, baseline_eligible=False)
            self.store.append(scope, name, now, sample)
            record = {"repository": name, "at": now, **sample}
            records.append(record)
            latest[name.casefold()] = record
            if sample["outcome"] in {"rate_limit", "request_limit", "deadline", "cancelled", "authentication"}:
                break
        rows = []
        incident_starts = [parsed(incident.get("started_at") or incident.get("created_at")) for incident in active]
        baseline_end = 0 if active and any(at is None for at in incident_starts) else min(
            (at.timestamp() for at in incident_starts if at), default=now)
        for name in names:
            item = latest.get(name.casefold())
            baseline = [entry for entry in records if entry["repository"].casefold() == name.casefold()
                        and entry.get("baseline_eligible") and entry["at"] < baseline_end
                        and entry["at"] >= baseline_end - 86400 and entry.get("outcome") == "observed"]
            baseline = baseline[-12:]
            fresh = bool(item and 0 <= now - item["at"] <= FRESH_SECONDS)
            median = statistics.median(entry["queued_runs"] for entry in baseline) if len(baseline) >= 3 else None
            elevated = bool(fresh and item and item.get("outcome") == "observed" and median is not None
                            and item["queued_runs"] >= median + 3 and (item.get("oldest_queued_age_seconds") or 0) >= 600)
            gap = ("metadata_unavailable" if name.casefold() not in available else
                   "never_observed" if item is None else "stale" if not fresh else
                   item["outcome"] if item["outcome"] != "observed" else None)
            rows.append({
                "repository": name, "sample": item, "fresh": fresh,
                "metadata_available": name.casefold() in available,
                "baseline_samples": len(baseline), "baseline_median_queued_runs": median,
                "signal": "observation_unavailable" if gap else "no_runs_in_sample" if item and item.get("sampled_runs") == 0 else
                          "elevated_queue_sample" if elevated else "insufficient_baseline" if median is None else "no_queue_increase_observed",
                "incident_overlap": bool(fresh and item and set(incident_ids).intersection(item.get("incident_ids", []))),
                "gap": gap,
                "recent_incident_samples": [entry for entry in records
                                           if entry["repository"].casefold() == name.casefold() and entry.get("incident_ids")][-3:],
            })
        return {
            "enabled": True, "persistent": self.store.path is not None, "rows": rows,
            "selected": len(names), "fresh_observed": sum(row["fresh"] and row["gap"] is None for row in rows),
            "gaps": sum(row["gap"] is not None for row in rows), "fresh_seconds": FRESH_SECONDS,
            "batch_size": BATCH_SIZE, "retention_days": 14, "record_limit": MAX_RECORDS,
            "limitation": LIMITATION,
        }
