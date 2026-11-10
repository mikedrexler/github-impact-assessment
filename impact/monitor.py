"""Single-flight polling with bounded in-memory history and graceful cancellation."""

from __future__ import annotations

import copy
import hashlib
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Callable

from .agent import Agent
from .assessment import assess, timestamp
from .client import Backoff, Client, Config, InventoryState, ProbeError, credentials, inventory, organization_name, repository_selection
from .history import EVIDENCE_LIMIT, HISTORY_LIMIT, incident_order, project_incidents
from .copilot_probe import CopilotProbe
from .selection_store import SelectionStore


class Monitor:
    def __init__(
        self, config: Config, *, client_factory: Callable | None = None,
        demo: bool = False, agent: Agent | None = None, selected: tuple[str, ...] | None = None,
        copilot_probe: CopilotProbe | None = None,
        selection_store: SelectionStore | None = None,
    ):
        self.config = config
        self.demo = demo
        self._factory = client_factory
        self._selection_store = selection_store if not demo else None
        # Injected clients stay network-isolated unless a planner is also injected.
        self._agent = agent if agent is not None else Agent.from_env(demo=demo or client_factory is not None)
        self._copilot = copilot_probe or CopilotProbe.from_env(isolated=demo or client_factory is not None)
        self._copilot_requested = False
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False
        self._requested = False
        self._last_request = float("-inf")
        self._next_due = 0.0
        self._offset = 0
        self._backoff = Backoff()
        self._inventory = InventoryState()
        self._discovery_latest = None
        self._discovery_error = None
        self._discovery_busy = False
        self._discovery_due = 0.0
        self._credential_key: bytes | None = None
        self._latest = None
        self._history: deque = deque(maxlen=10)
        self._error = None
        self._auth = "Not checked yet"
        self._past = None
        self._past_error = None
        self._past_updated_at = None
        self._incident_evidence: dict[str, dict] = {}
        self._selected = repository_selection(list(selected), config.max_repositories) if selected else selected
        self._discovery_opted_in = selected is None
        self._pending_selection: dict | None = None
        self._organization: str | None = None
        self._organization_inventory = InventoryState()
        self._organization_report = None
        self._organization_error = None
        self._organization_error = None
        self._organization_busy = False
        self._organization_due = float("inf")
        self._organization_last_request = float("-inf")

    def discover_organization(self, organization: str) -> str:
        organization = organization_name(organization)
        with self._lock:
            if self.stop_event.is_set():
                return "stopped"
            if self._organization_busy:
                return "already_running"
            if self._organization == organization and self._organization_due != float("inf"):
                return "already_running"
            if time.monotonic() - self._organization_last_request < 10:
                return "throttled"
            self._organization_last_request = time.monotonic()
            self._organization = organization
            self._organization_inventory = InventoryState()
            self._organization_report = None
            self._organization_error = None
            self._organization_due = 0.0
            self._wake.set()
        return "accepted"

    def _discover_organization(self):
        with self._lock:
            self._organization_busy = True
            organization = self._organization
        delay = float("inf")
        try:
            if organization is None:
                raise ValueError("Organization required")
            client = self._new_client(replace(
                self.config, cycle_seconds=self.config.discovery_seconds,
                max_requests=min(self.config.max_requests, self.config.discovery_requests),
            ))
            report = inventory(
                client, organization=organization, state=self._organization_inventory,
                deadline=time.monotonic() + self.config.discovery_seconds,
                request_limit=min(self.config.max_requests, self.config.discovery_requests),
            )
            reason = (report["reason"] or {}).get("kind")
            if reason in {"inventory_deadline", "inventory_request_limit", "deadline", "request_limit"}:
                delay = self.config.discovery_resume_seconds
            elif reason == "rate_limit":
                with self._backoff.lock:
                    delay = max(self.config.poll_seconds, self._backoff.until - time.monotonic())
            elif reason and reason != "inventory_limit":
                delay = self.config.poll_seconds
            report["updated_at"] = timestamp()
            with self._lock:
                self._organization_report = report
                self._organization_error = None
        except Exception as error:
            with self._lock:
                self._organization_error = {
                    "kind": type(error).__name__,
                    "message": "Organization discovery failed; results may be incomplete. No scope change applied.",
                }
            print(f"Organization discovery failed ({type(error).__name__}); no raw response logged", file=sys.stderr)
            delay = self.config.poll_seconds
        finally:
            with self._lock:
                self._organization_busy = False
                self._organization_due = time.monotonic() + delay

    def select_repositories(self, mode: str, repositories: list) -> str:
        if not isinstance(mode, str) or mode not in {"selected", "all", "paused"}:
            raise ValueError("Scope mode must be selected, all or paused")
        if mode == "selected":
            names = repository_selection(repositories, self.config.max_repositories)
        elif repositories:
            raise ValueError("All/paused scope must not include repositories")
        else:
            names = ()
        with self._lock:
            if self.stop_event.is_set():
                return "stopped"
            requested = {"mode": mode, "repositories": list(names)}
            active = {
                "mode": "all" if self._selected is None else "selected" if self._selected else "paused",
                "repositories": list(self._selected or ()),
            }
            if self._selection_store:
                self._selection_store.save(mode, list(names))
            if requested == (self._pending_selection or active):
                return "unchanged"
            self._pending_selection = requested
            if mode == "all":
                self._discovery_opted_in = True
            self._wake.set()
        return "accepted"

    def _apply_selection(self):
        """Only the scheduler changes scope, after any in-flight batch completes."""
        with self._lock:
            if self._pending_selection is None:
                return
            pending = self._pending_selection
            self._pending_selection = None
            self._selected = None if pending["mode"] == "all" else tuple(pending["repositories"])
            self._inventory = InventoryState()
            self._offset = 0
            self._latest = None
            self._history.clear()
            self._error = None
            self._discovery_latest = None
            self._discovery_error = None
            self._discovery_due = self._next_due = 0.0
            self._requested = False

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Monitor already started")
            self._thread = threading.Thread(target=self._loop, name="impact-monitor", daemon=True)
            self._thread.start()

    def refresh(self) -> str:
        with self._lock:
            if self.stop_event.is_set():
                return "stopped"
            if self._busy or self._requested:
                return "already_running"
            if time.monotonic() - self._last_request < 10:
                return "throttled"
            self._requested = True
            self._last_request = time.monotonic()
            self._wake.set()
            return "accepted"

    def request_copilot_probe(self) -> str:
        with self._lock:
            if self.stop_event.is_set():
                return "stopped"
            if self.demo or self._copilot.snapshot()["configuration"] != "ready":
                return "not_configured"
            if self._busy or self._requested or self._copilot_requested:
                return "already_running"
            if self._pending_selection or not self._latest or not any(
                row["adapter"] == "copilot_model" for row in self._latest["checks"]
            ):
                return "no_scoped_model_incident"
            self._copilot_requested = True
            self._requested = True
            self._wake.set()
            return "accepted"

    def _loop(self):
        while not self.stop_event.is_set():
            self._apply_selection()
            with self._lock:
                due = self._requested or time.monotonic() >= self._next_due
                if due:
                    self._busy = True
                    self._requested = False
            if due:
                self._cycle()
            elif time.monotonic() >= self._organization_due:
                self._discover_organization()
            elif time.monotonic() >= self._discovery_due:
                self._discover()
            with self._lock:
                delay = max(0.01, min(self._next_due, self._discovery_due, self._organization_due) - time.monotonic())
            self._wake.wait(delay)
            self._wake.clear()

    def _new_client(self, config: Config):
        if self._factory:
            client = self._factory(config, self.stop_event)
            auth = "Deterministic fixture; no credentials or network" if self.demo else "Injected client"
        else:
            token, auth = credentials()
            credential_key = hashlib.sha256(token.encode()).digest() if token else None
            if credential_key != self._credential_key:
                self._organization_inventory = InventoryState()
                self._inventory = InventoryState()
                self._offset = 0
                self._credential_key = credential_key
                with self._lock:
                    self._latest = None
                    self._history.clear()
                    self._discovery_latest = None
                    self._discovery_error = None
                    self._discovery_due = 0.0
                    self._next_due = 0.0
                    self._organization_report = None
                    if self._organization:
                        self._organization_due = 0.0
                    self._incident_evidence.clear()
            client = Client(token, config, self.stop_event, backoff=self._backoff)
        with self._lock:
            self._auth = auth
        return client

    def _discover(self):
        """Paced discovery-only work; never repeats incident probes or history."""
        with self._lock:
            self._discovery_busy = True
        delay = self.config.poll_seconds
        try:
            client = self._new_client(replace(
                self.config, cycle_seconds=self.config.discovery_seconds,
                max_requests=min(self.config.max_requests, self.config.discovery_requests),
            ))
            report = inventory(
                client, state=self._inventory, selected=self._selected,
                deadline=time.monotonic() + self.config.discovery_seconds,
                request_limit=min(self.config.max_requests, self.config.discovery_requests),
            )
            reason = (report["reason"] or {}).get("kind")
            if reason in {"inventory_deadline", "inventory_request_limit", "deadline", "request_limit"}:
                delay = self.config.discovery_resume_seconds
                time_budget = reason in {"inventory_deadline", "deadline"}
                report["reason"] = {
                    "kind": "inventory_deadline" if time_budget else "inventory_request_limit",
                    "message": f"Discovery {'time' if time_budget else 'request'} checkpoint saved; background continuation scheduled after {delay}s",
                }
            if reason == "rate_limit":
                with self._backoff.lock:
                    delay = max(delay, self._backoff.until - time.monotonic())
            report["updated_at"] = timestamp()
            report["requests_used"] = client.requests
            with self._lock:
                self._discovery_latest = report
                self._discovery_error = None
        except Exception as error:
            with self._lock:
                self._discovery_error = {
                    "at": timestamp(), "kind": type(error).__name__,
                    "message": "Repository discovery failed unexpectedly; cached inventory may be stale.",
                }
            print(f"Discovery failed ({type(error).__name__}); no raw response logged", file=sys.stderr)
        finally:
            with self._lock:
                self._discovery_busy = False
                self._discovery_due = time.monotonic() + delay

    def _cycle(self):
        self._apply_selection()
        with self._lock:
            run_copilot = self._copilot_requested
            self._copilot_requested = False
        try:
            if self._discovery_latest is None:
                self._discover()
            client = self._new_client(self.config)
            if self._discovery_latest is None and self._discovery_error is None:
                # Credentials changed after the last discovery snapshot.
                self._discover()
                client = self._new_client(self.config)
            with self._lock:
                found = copy.deepcopy(self._discovery_latest)
                discovery_error = self._discovery_error
            if found is None:
                raise RuntimeError("Discovery snapshot unavailable")
            if discovery_error:
                found["complete"] = False
                found["reason"] = {"kind": "discovery_error", "message": discovery_error["message"]}
            report = assess(client, self._offset, inventory_report=found, agent=self._agent,
                            copilot_probe=self._copilot, run_copilot=run_copilot)
            self._collect_past(client)
            report["requests_used"] = client.requests
            with self._lock:
                self._offset = report.pop("next_offset")
                self._latest = report
                self._history.appendleft({
                    "completed_at": report["completed_at"], "counts": report["counts"],
                    "discovered": report["inventory"]["discovered"],
                    "coverage_complete": report["coverage"]["complete"],
                    "incidents": len(report["incidents"]),
                })
                self._error = None
                for incident in report["incidents"]:
                    rows = [row for row in report["checks"] if row["incident_id"] == incident["id"]]
                    if not rows:
                        continue
                    self._incident_evidence.pop(incident["id"], None)
                    self._incident_evidence[incident["id"]] = {
                        "completed_at": report["completed_at"], "checks": copy.deepcopy(rows),
                        "counts": {key: sum(row["classification"] == key for row in rows) for key in report["counts"]},
                        "coverage": copy.deepcopy(report["coverage"]),
                        "inventory_discovered": report["inventory"]["discovered"],
                    }
                while len(self._incident_evidence) > EVIDENCE_LIMIT:
                    del self._incident_evidence[next(iter(self._incident_evidence))]
        except Exception as error:
            # Last-resort scheduler boundary: keep the prior timestamped snapshot,
            # surface failure, and never print exception text containing secrets.
            with self._lock:
                self._error = {"at": timestamp(), "kind": type(error).__name__,
                               "message": "Assessment failed unexpectedly; prior snapshot is stale. See troubleshooting."}
            print(f"Assessment failed ({type(error).__name__}); no raw response logged", file=sys.stderr)
        finally:
            with self._lock:
                self._busy = False
                self._next_due = time.monotonic() + self.config.poll_seconds

    def _collect_past(self, client):
        try:
            incidents = project_incidents(client.past_incidents())
            with self._lock:
                self._past = incidents
                self._past_updated_at = timestamp()
                self._past_error = None
        except ProbeError as error:
            with self._lock:
                self._past_error = {"kind": error.kind, "message": error.message, "at": timestamp()}
        except Exception as error:
            with self._lock:
                self._past_error = {
                    "kind": type(error).__name__, "at": timestamp(),
                    "message": "Incident history refresh failed unexpectedly; prior history may be stale.",
                }
            print(f"Incident history failed ({type(error).__name__}); no raw response logged", file=sys.stderr)

    def past_snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "demo": self.demo, "updated_at": self._past_updated_at,
                "error": self._past_error, "available": self._past is not None,
                "poll_seconds": self.config.poll_seconds, "cycle_seconds": self.config.cycle_seconds,
                "incidents": sorted([
                    {**item, "assessment": self._incident_evidence.get(item["id"])}
                    for item in self._past or []
                ], key=incident_order, reverse=True),
                "scope": f"Latest public Status history response only (up to {HISTORY_LIMIT} incidents, resolved/postmortem only); not an exhaustive archive. Latest observed assessment retained for up to {EVIDENCE_LIMIT} incidents in server memory, cleared on restart or credential change. No retrospective probes. Offline test results are not imported.",
            })

    def snapshot(self) -> dict:
        with self._lock:
            remaining = max(0, self._next_due - time.monotonic())
            next_at = datetime.fromtimestamp(time.time() + remaining, timezone.utc).isoformat()
            return copy.deepcopy({
                "demo": self.demo, "busy": self._busy, "refresh_pending": self._requested,
                "stopped": self.stop_event.is_set(), "authentication": self._auth,
                "next_poll_at": next_at, "server_time": timestamp(),
                "config": asdict(self.config), "latest": self._latest,
                "copilot_probe": self._copilot.snapshot(),
                "organization_discovery": {
                    "organization": self._organization, "busy": self._organization_busy,
                    "pending": self._organization_due != float("inf"),
                    "latest": self._organization_report, "error": self._organization_error,
                },
                "selection": {
                    "mode": "all" if self._selected is None else "selected" if self._selected else "paused",
                    "repositories": list(self._selected or ()),
                    "pending": self._pending_selection,
                    "discovery_opted_in": self._discovery_opted_in,
                    "persistent": self._selection_store is not None,
                    "limit": min(self.config.max_repositories, 200),
                },
                "history": list(self._history), "error": self._error,
                "discovery": {
                    "busy": self._discovery_busy,
                    "next_batch_at": datetime.fromtimestamp(
                        time.time() + max(0, self._discovery_due - time.monotonic()), timezone.utc,
                    ).isoformat(),
                    "latest": self._discovery_latest, "error": self._discovery_error,
                },
            })

    def stop(self):
        self.stop_event.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=self.config.timeout_seconds + 5)
