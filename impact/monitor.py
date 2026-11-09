"""Single-flight polling with bounded in-memory history and graceful cancellation."""

from __future__ import annotations

import copy
import hashlib
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

from .assessment import assess, timestamp
from .client import Backoff, Client, Config, InventoryState, credentials


class Monitor:
    def __init__(self, config: Config, *, client_factory: Callable | None = None, demo: bool = False):
        self.config = config
        self.demo = demo
        self._factory = client_factory
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
        self._credential_key: bytes | None = None
        self._latest = None
        self._history: deque = deque(maxlen=10)
        self._error = None
        self._auth = "Not checked yet"

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
            with self._lock:
                due = self._requested or time.monotonic() >= self._next_due
                if due:
                    self._busy = True
                    self._requested = False
            if due:
                self._cycle()
            with self._lock:
                delay = max(0.01, self._next_due - time.monotonic())
            self._wake.wait(delay)
            self._wake.clear()

    def _cycle(self):
        try:
            if self._factory:
                client = self._factory(self.config, self.stop_event)
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
                client = Client(token, self.config, self.stop_event, backoff=self._backoff)
            with self._lock:
                self._auth = auth
            report = assess(client, self._offset, self._inventory)
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
                "history": list(self._history), "error": self._error,
            })

    def stop(self):
        self.stop_event.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=self.config.timeout_seconds + 5)
