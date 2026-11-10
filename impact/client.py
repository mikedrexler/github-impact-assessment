"""Bounded GET-only clients. Credentials never cross the GitHub API boundary."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

STATUS_URL = "https://www.githubstatus.com/api/v2/summary.json"
INCIDENTS_URL = "https://www.githubstatus.com/api/v2/incidents.json"
API_ROOT = "https://api.github.com"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProbeError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def credentials() -> tuple[str | None, str]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        source = "environment"
    else:
        try:
            response = subprocess.run(
                ["gh", "auth", "token", "--hostname", "github.com"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, "No environment token or usable gh authentication"
        token = response.stdout.strip() if response.returncode == 0 else None
        source = "gh authentication"
    if not token or any(char.isspace() for char in token):
        return None, "No valid credential available; use existing gh authentication"
    return token, source


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Config:
    poll_seconds: int = 300
    timeout_seconds: int = 10
    cycle_seconds: int = 120
    max_requests: int = 500
    max_repositories: int = 10000
    max_check_repositories: int = 200
    max_assessments: int = 2000
    discovery_seconds: int = 15
    discovery_requests: int = 10
    discovery_resume_seconds: int = 5
    workers: int = 4
    port: int = 8765
    github_requests_hour: int = 600
    github_requests_minute: int = 30

    @classmethod
    def from_env(cls) -> Config:
        bounds = {
            "poll_seconds": (60, 86400), "timeout_seconds": (1, 60),
            "cycle_seconds": (10, 1800), "max_requests": (1, 10000),
            "max_repositories": (1, 100000), "max_check_repositories": (1, 10000),
            "max_assessments": (1, 20000),
            "discovery_seconds": (1, 60), "discovery_requests": (1, 100),
            "discovery_resume_seconds": (1, 300),
            "workers": (1, 16), "port": (1024, 65535),
            "github_requests_hour": (1, 1800), "github_requests_minute": (1, 60),
        }
        values = {}
        for name, (low, high) in bounds.items():
            value = os.environ.get(f"IMPACT_{name.upper()}")
            if value is not None:
                try:
                    number = int(value)
                except ValueError:
                    raise ValueError(f"IMPACT_{name.upper()} must be an integer") from None
                if not low <= number <= high:
                    raise ValueError(f"IMPACT_{name.upper()} must be {low}..{high}")
                values[name] = number
        return cls(**values)


class Backoff:
    """Shared across cycles, so manual refresh cannot bypass rate-limit cooldown."""

    def __init__(self):
        self.until = 0.0
        self.lock = threading.Lock()
        self.admissions: deque[float] = deque()
        self.remaining: int | None = None
        self.reset = 0.0
        self.headroom = 50
        self.reason = None

    def reserve(self, now: float, config: Config):
        """Caller holds lock; every attempt, including retries, consumes a slot."""
        while self.admissions and self.admissions[0] <= now - 3600:
            self.admissions.popleft()
        if now < self.until:
            raise ProbeError("rate_limit", "GitHub reads deferred by shared rate budget/backoff")
        if self.remaining is not None and self.remaining <= self.headroom and self.reset > time.time():
            self.until = now + self.reset - time.time() + 2
            self.reason = "provider_quota_reserve"
            raise ProbeError("rate_limit", "GitHub quota reserve reached; checks deferred")
        minute = [at for at in self.admissions if at > now - 60]
        if len(self.admissions) >= config.github_requests_hour or len(minute) >= config.github_requests_minute:
            self.until = max(
                self.admissions[0] + 3600 if len(self.admissions) >= config.github_requests_hour else now,
                minute[0] + 60 if len(minute) >= config.github_requests_minute else now,
            )
            self.reason = "local_request_budget"
            raise ProbeError("rate_limit", "Local GitHub request budget reached; checks deferred")
        self.admissions.append(now)
        if self.remaining is not None:
            self.remaining = max(0, self.remaining - 1)

    def observe(self, headers: dict, now: float):
        headers = {key.lower(): value for key, value in headers.items()}
        try:
            remaining = int(headers["x-ratelimit-remaining"])
            reset = float(headers["x-ratelimit-reset"])
            limit = int(headers.get("x-ratelimit-limit", "500"))
            if remaining < 0 or limit < 1:
                return
        except (KeyError, ValueError, TypeError):
            return
        if not math.isfinite(reset):
            return
        with self.lock:
            # Concurrent responses can arrive out of order. Never raise our
            # remaining estimate within one provider quota window.
            if reset > self.reset:
                self.remaining, self.reset = remaining, reset
            elif reset == self.reset:
                self.remaining = min(self.remaining if self.remaining is not None else remaining, remaining)
            reserve = min(50, max(1, limit // 10))
            self.headroom = reserve
            if self.remaining is not None and self.remaining <= reserve:
                self.until = max(self.until, now + max(60, self.reset - time.time() + 2))
                self.reason = "provider_quota_reserve"

    def snapshot(self) -> dict:
        with self.lock:
            now = time.monotonic()
            return {
                "deferred_seconds": max(0, round(self.until - now)),
                "reason": self.reason if self.until > now else None,
                "provider_remaining_estimate": self.remaining,
                "attempts_last_hour": sum(at > now - 3600 for at in self.admissions),
            }


class Client:
    def __init__(
        self, token: str | None, config: Config, stop: threading.Event,
        *, opener=None, clock: Callable[[], float] = time.monotonic,
        backoff: Backoff | None = None,
    ):
        self._token = token
        self.config = config
        self.stop = stop
        self.clock = clock
        self.deadline = clock() + config.cycle_seconds
        self.requests = 0
        self.access_warnings: set[str] = set()
        self._lock = threading.Lock()
        self.backoff = backoff or Backoff()
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def _reserve(self, github: bool):
        with self._lock:
            if self.stop.is_set():
                raise ProbeError("cancelled", "Monitoring stopped")
            if self.clock() >= self.deadline:
                raise ProbeError("deadline", "Assessment time budget exhausted")
            if self.requests >= self.config.max_requests:
                raise ProbeError("request_limit", "Assessment request budget exhausted")
            if github and not self._token:
                raise ProbeError("authentication", "No configured GitHub credential")
            if github:
                with self.backoff.lock:
                    self.backoff.reserve(self.clock(), self.config)
            self.requests += 1

    def _json(
        self, url: str, *, github: bool, deadline: float | None = None, attempts: int = 3,
    ) -> tuple[Any, dict]:
        for attempt in range(attempts):
            if deadline is not None and self.clock() >= deadline:
                raise ProbeError("deadline", "Read-only planning budget exhausted")
            self._reserve(github)
            headers = {"Accept": "application/json", "User-Agent": "github-impact-assessment/0.1"}
            if github:
                headers["Authorization"] = f"Bearer {self._token}"
                headers["X-GitHub-Api-Version"] = "2022-11-28"
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                timeout = max(0.1, min(
                    self.config.timeout_seconds, self.deadline - self.clock(),
                    deadline - self.clock() if deadline is not None else self.config.timeout_seconds,
                ))
                with self._opener.open(request, timeout=timeout) as response:
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                    response_headers = dict(response.headers.items())
                if github:
                    self.backoff.observe(response_headers, self.clock())
                    sso = next((value for key, value in response_headers.items() if key.lower() == "x-github-sso"), "")
                    if "partial-results" in sso or "required" in sso:
                        with self._lock:
                            self.access_warnings.add("SSO authorization restricted API results; repository coverage is partial")
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ProbeError("response_limit", "Response exceeds the safe size limit")
                try:
                    return json.loads(data), response_headers
                except (ValueError, UnicodeDecodeError):
                    raise ProbeError("invalid_response", "Service returned invalid JSON") from None
            except urllib.error.HTTPError as error:
                status = error.code
                if github:
                    self.backoff.observe(dict(error.headers.items()), self.clock())
                retry_after = error.headers.get("Retry-After")
                remaining = error.headers.get("X-RateLimit-Remaining")
                # Conservatively back off on 403: secondary limits do not always
                # include Retry-After or an exhausted primary quota.
                limited = status == 429 or (status == 403 and (retry_after or remaining == "0"))
                if github and (limited or status == 403):
                    try:
                        delay = float(retry_after or "60")
                    except ValueError:
                        try:
                            delay = parsedate_to_datetime(retry_after or "").timestamp() - time.time()
                        except (TypeError, ValueError, OverflowError):
                            delay = 60
                    if not math.isfinite(delay):
                        delay = 60
                    try:
                        reset_delay = float(error.headers.get("X-RateLimit-Reset", "0")) - time.time()
                    except ValueError:
                        reset_delay = 0
                    with self.backoff.lock:
                        self.backoff.until = max(self.backoff.until, self.clock() + max(60, delay, reset_delay))
                        self.backoff.reason = "provider_rate_or_permission_backoff"
                error.close()
                if limited:
                    service = "GitHub API" if github else "Status API"
                    raise ProbeError("rate_limit", f"{service} rate limited; checks deferred") from None
                if status >= 500 and attempt < attempts - 1:
                    self._wait(attempt)
                    continue
                kind = {
                    401: "authentication", 403: "permission_or_secondary_limit",
                    404: "not_found_or_hidden", 408: "timeout", 409: "conflict",
                }.get(status, "service_error")
                raise ProbeError(kind, f"Read-only endpoint returned HTTP {status}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < attempts - 1:
                    self._wait(attempt)
                    continue
                raise ProbeError("network", "Network/timeout failure after bounded attempts") from None
        raise AssertionError("unreachable")

    def _wait(self, attempt: int):
        if self.stop.wait(min(2 ** attempt, max(0, self.deadline - self.clock()))):
            raise ProbeError("cancelled", "Monitoring stopped")

    def status(self) -> dict:
        data, _ = self._json(STATUS_URL, github=False)
        if not isinstance(data, dict) or not isinstance(data.get("components"), list) or not isinstance(data.get("incidents"), list):
            raise ProbeError("invalid_response", "Status API response lacks components or incidents")
        for component in data["components"]:
            if not isinstance(component, dict) or not isinstance(component.get("id"), str) or not isinstance(component.get("name"), str) or not isinstance(component.get("status"), str):
                raise ProbeError("invalid_response", "Status API returned an invalid component")
        for incident in data["incidents"]:
            if not isinstance(incident, dict) or not isinstance(incident.get("id"), str) or not isinstance(incident.get("components", []), list):
                raise ProbeError("invalid_response", "Status API returned an invalid incident")
        return data

    def get(self, path: str) -> Any:
        return self._get(path)

    def get_once(self, path: str, deadline: float) -> Any:
        """One planner read, charged to the normal cycle budget; no retries."""
        return self._get(path, deadline=deadline, attempts=1)

    def _get(self, path: str, *, deadline: float | None = None, attempts: int = 3) -> Any:
        if not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
            raise ProbeError("invalid_path", "Only fixed GitHub API paths are allowed")
        data, _ = self._json(API_ROOT + path, github=True, deadline=deadline, attempts=attempts)
        return data

    def past_incidents(self) -> dict:
        data, _ = self._json(INCIDENTS_URL, github=False)
        if not isinstance(data, dict) or not isinstance(data.get("incidents"), list):
            raise ProbeError("invalid_response", "Status history response lacks incidents")
        return data


def repository_path(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", name) or any(
        part in {".", ".."} for part in name.split("/")
    ):
        raise ProbeError("invalid_response", "GitHub returned an invalid repository name")
    return "/repos/" + name


def repository_selection(value: Any, limit: int) -> tuple[str, ...]:
    """Validate an explicit list; never interpret URLs, query strings or paths."""
    if not isinstance(value, list) or not value or len(value) > min(limit, 200):
        raise ValueError(f"Select 1..{min(limit, 200)} repositories")
    names: dict[str, str] = {}
    for name in value:
        if not isinstance(name, str):
            raise ValueError("Each repository must be an owner/repo name")
        try:
            repository_path(name)
        except ProbeError:
            raise ValueError("Use exact owner/repo names, not URLs or paths") from None
        names.setdefault(name.casefold(), name)
    return tuple(names.values())


@dataclass
class InventoryState:
    """Single-monitor, memory-only cursor and last completed inventory."""

    repositories: dict[str, dict] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    next_page: int = 1
    scanning: bool = False
    started_at: str | None = None
    completed_at: str | None = None
    sso_partial: bool = False
    selection_errors: list[dict] = field(default_factory=list)


def selected_inventory(
    client: Client, names: tuple[str, ...], state: InventoryState,
    deadline: float | None, request_limit: int | None, writable: bool = False,
) -> dict:
    if not state.scanning:
        state.pending = {}
        state.next_page = 1
        state.scanning = True
        state.started_at = datetime.now(timezone.utc).isoformat()
        state.selection_errors = []
    reason = None
    while state.next_page <= len(names):
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise ProbeError("inventory_deadline", "Selected repository validation checkpoint saved")
            if request_limit is not None and client.requests >= request_limit:
                raise ProbeError("inventory_request_limit", "Selected repository request checkpoint saved")
            name = names[state.next_page - 1]
            row = client.get(repository_path(name))
            if not isinstance(row, dict) or not isinstance(row.get("full_name"), str):
                raise ProbeError("invalid_response", "Selected repository metadata is invalid")
            if row["full_name"].casefold() != name.casefold():
                raise ProbeError("repository_mismatch", "Repository identity differs from the explicit selection; update the selection")
            if writable and not has_write_access(row):
                raise ProbeError("write_access_required", "Credential must report write, maintain or admin access; repository excluded")
            state.pending[name] = {
                "name": name, "url": f"https://github.com/{name}",
                "archived": bool(row.get("archived")),
                "write_access": has_write_access(row),
                "has_issues": row.get("has_issues") if isinstance(row.get("has_issues"), bool) else None,
                "has_pages": row.get("has_pages") if isinstance(row.get("has_pages"), bool) else None,
            }
            if getattr(client, "access_warnings", set()):
                state.selection_errors.append({
                    "repository": name, "kind": "sso_partial_results",
                    "message": "SSO restrictions reported; selected repository coverage is partial",
                })
            state.next_page += 1
        except ProbeError as error:
            if error.kind in {"inventory_deadline", "inventory_request_limit", "deadline", "request_limit", "rate_limit", "cancelled"}:
                reason = {"kind": error.kind, "message": error.message}
                break
            state.selection_errors.append({
                "repository": names[state.next_page - 1], "kind": error.kind, "message": error.message,
            })
            state.next_page += 1
    if state.next_page > len(names):
        state.scanning = False
        state.repositories = state.pending.copy()
        if not state.selection_errors:
            state.completed_at = datetime.now(timezone.utc).isoformat()
    repositories = list(state.pending.values())
    if not names:
        reason = {"kind": "scope_paused", "message": "Repository checks paused; select repositories or explicitly enable account-wide discovery"}
    return {
        "repositories": repositories, "complete": reason is None and not state.selection_errors,
        "reason": reason or ({"kind": "selected_repository_errors", "message": "Some selected repositories could not be validated; see repository errors"} if state.selection_errors else None),
        "repository_errors": list(state.selection_errors),
        "discovered": len(repositories), "selected_count": len(names), "next_page": None,
        "next_repository": state.next_page if state.scanning else None,
        "scan_started_at": state.started_at, "last_completed_at": state.completed_at,
        "scan_discovered": len(repositories), "cached_repositories": 0,
        "scope": f"Explicit selection only: {len(names)} repositories. No account or organization repository enumeration.",
        "access_caveat": ("Only selected repositories with verified write/maintain/admin permission are assessed. " if writable else "Only successfully validated selected repositories are assessed. ")
                         + "Missing access, hidden/renamed repositories and failed reads remain gaps, not unaffected.",
    }


def organization_name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?", value):
        raise ValueError("Use an exact GitHub organization login")
    return value.lower()


def has_write_access(row: dict) -> bool:
    permissions = row.get("permissions")
    return isinstance(permissions, dict) and any(
        permissions.get(role) is True for role in ("push", "maintain", "admin")
    )


def inventory(
    client: Client, *, deadline: float | None = None,
    request_limit: int | None = None, state: InventoryState | None = None,
    selected: tuple[str, ...] | None = None,
    organization: str | None = None,
    writable: bool = False,
) -> dict:
    if organization is not None:
        organization = organization_name(organization)
        if selected is not None:
            raise ValueError("Organization discovery and selected validation are separate")
    state = state if state is not None else InventoryState()
    if selected is not None:
        return selected_inventory(client, selected, state, deadline, request_limit, writable)
    if not state.scanning:
        state.pending = {}
        state.next_page = 1
        state.scanning = True
        state.started_at = datetime.now(timezone.utc).isoformat()
        state.sso_partial = False
    reason = None
    while True:
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise ProbeError("inventory_deadline", "Discovery batch time budget reached; progress saved for automatic continuation")
            if request_limit is not None and client.requests >= request_limit:
                raise ProbeError("inventory_request_limit", "Discovery batch request budget reached; progress saved for automatic continuation")
            path = (
                f"/orgs/{organization}/repos?per_page=100&page={state.next_page}&type=all&sort=full_name&direction=asc"
                if organization else
                f"/user/repos?per_page=100&page={state.next_page}&affiliation=owner,collaborator,organization_member&sort=full_name&direction=asc"
            )
            rows = client.get(path)
            if not isinstance(rows, list):
                raise ProbeError("invalid_response", "Repository enumeration was not a list")
            page_repositories = {}
            denied = set()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("full_name"), str):
                    raise ProbeError("invalid_response", "Invalid repository in enumeration")
                name = row["full_name"]
                repository_path(name)
                if organization and name.split("/")[0].casefold() != organization:
                    raise ProbeError("repository_mismatch", "Organization enumeration returned an out-of-scope repository")
                if writable and not has_write_access(row):
                    denied.add(name.casefold())
                    continue
                page_repositories[name] = {
                    "name": name, "url": f"https://github.com/{name}",
                    "archived": bool(row.get("archived")),
                    "write_access": has_write_access(row),
                    "has_issues": row.get("has_issues") if isinstance(row.get("has_issues"), bool) else None,
                    "has_pages": row.get("has_pages") if isinstance(row.get("has_pages"), bool) else None,
                }
            if denied:
                for entries in (state.repositories, state.pending, page_repositories):
                    for cached_name in list(entries):
                        if cached_name.casefold() in denied:
                            del entries[cached_name]
            state.sso_partial |= bool(getattr(client, "access_warnings", set()))
            for name, repository in page_repositories.items():
                if name not in state.pending and len(state.pending) >= client.config.max_repositories:
                    reason = {"kind": "inventory_limit", "message": "Configured repository inventory limit reached"}
                    break
                state.pending[name] = repository
            if reason:
                # Retry this page after a capacity increase; never skip its tail.
                break
            state.next_page += 1
            if len(rows) < 100:
                if state.sso_partial:
                    # Do not prune an older inventory from a restricted traversal.
                    reason = {"kind": "sso_partial_results", "message": "SSO restrictions reported by GitHub; enumeration is partial"}
                    combined = {**state.repositories, **state.pending}
                    state.repositories = dict(list(combined.items())[:client.config.max_repositories])
                else:
                    state.repositories = state.pending
                    state.completed_at = datetime.now(timezone.utc).isoformat()
                state.scanning = False
                break
        except ProbeError as error:
            reason = {"kind": error.kind, "message": error.message}
            break
    # Keep previously discovered repositories usable while revalidation proceeds,
    # but prune removed entries only after an unrestricted traversal completes.
    combined = {**state.repositories, **state.pending}
    repositories = sorted(combined.values(), key=lambda repo: repo["name"].casefold())
    # Old and pending generations can differ; bound the exposed inventory too.
    if len(repositories) > client.config.max_repositories:
        repositories = repositories[:client.config.max_repositories]
        reason = reason or {"kind": "inventory_limit", "message": "Combined cached inventory exceeds configured capacity"}
    return {
        "repositories": repositories, "complete": reason is None, "reason": reason,
        "discovered": len(repositories),
        "next_page": state.next_page if state.scanning else None,
        "scan_started_at": state.started_at, "last_completed_at": state.completed_at,
        "scan_discovered": len(state.pending),
        "cached_repositories": sum(repo["name"] not in state.pending for repo in repositories),
        "scope": ("Writable-only catalog: credential-reported write, maintain or admin access. " if writable else "")
                 + (f"Organization-only discovery: {organization}. No account-wide repository enumeration." if organization else "Credential-visible owned, collaborator and organization-member repositories. Invisible/SSO-restricted repositories and unrelated public repositories are not enumerable here."),
        "access_caveat": "Discovery completion is not proof of full account access: token selection, SSO and permissions can silently hide repositories. Cached entries may have changed access.",
    }
