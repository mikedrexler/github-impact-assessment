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
from .client import Backoff, Client, Config, InventoryState, credentials, inventory, repository_selection


class Monitor:
    def __init__(
        self, config: Config, *, client_factory: Callable | None = None,
        demo: bool = False, agent: Agent | None = None, selected: tuple[str, ...] | None = None,
    ):
        self.config = config
        self.demo = demo
        self._factory = client_factory
        # Injected clients stay network-isolated unless a planner is also injected.
        self._agent = agent if agent is not None else Agent.from_env(demo=demo or client_factory is not None)
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
        self._selected = repository_selection(list(selected), config.max_repositories) if selected else selected
        self._pending_selection: dict | None = None

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
            if requested == (self._pending_selection or active):
                return "unchanged"
            self._pending_selection = requested
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
            elif time.monotonic() >= self._discovery_due:
                self._discover()
            with self._lock:
                delay = max(0.01, min(self._next_due, self._discovery_due) - time.monotonic())
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
            scope_args = {"selected": self._selected} if self._selected is not None else {}
            report = inventory(
                client, state=self._inventory,
                deadline=time.monotonic() + self.config.discovery_seconds,
                request_limit=min(self.config.max_requests, self.config.discovery_requests),
                **scope_args,
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
            report = assess(client, self._offset, inventory_report=found, agent=self._agent)
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

    def snapshot(self) -> dict:
        with self._lock:
            remaining = max(0, self._next_due - time.monotonic())
            next_at = datetime.fromtimestamp(time.time() + remaining, timezone.utc).isoformat()
            return copy.deepcopy({
                "demo": self.demo, "busy": self._busy, "refresh_pending": self._requested,
                "stopped": self.stop_event.is_set(), "authentication": self._auth,
                "next_poll_at": next_at, "server_time": timestamp(),
                "config": asdict(self.config), "latest": self._latest,
                "selection": {
                    "mode": "all" if self._selected is None else "selected" if self._selected else "paused",
                    "repositories": list(self._selected or ()),
                    "pending": self._pending_selection,
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
