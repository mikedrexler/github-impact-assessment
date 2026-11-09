"""Bounded GET-only clients. Credentials never cross the GitHub API boundary."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

STATUS_URL = "https://www.githubstatus.com/api/v2/summary.json"
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
    workers: int = 4
    port: int = 8765

    @classmethod
    def from_env(cls) -> Config:
        bounds = {
            "poll_seconds": (60, 86400), "timeout_seconds": (1, 60),
            "cycle_seconds": (10, 1800), "max_requests": (1, 10000),
            "max_repositories": (1, 100000), "max_check_repositories": (1, 10000),
            "max_assessments": (1, 20000),
            "workers": (1, 16), "port": (1024, 65535),
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
            with self.backoff.lock:
                if github and self.clock() < self.backoff.until:
                    raise ProbeError("rate_limit", "GitHub API backoff is active")
            if self.requests >= self.config.max_requests:
                raise ProbeError("request_limit", "Assessment request budget exhausted")
            if github and not self._token:
                raise ProbeError("authentication", "No configured GitHub credential")
            self.requests += 1

    def _json(self, url: str, *, github: bool) -> tuple[Any, dict]:
        for attempt in range(3):
            self._reserve(github)
            headers = {"Accept": "application/json", "User-Agent": "github-impact-assessment/0.1"}
            if github:
                headers["Authorization"] = f"Bearer {self._token}"
                headers["X-GitHub-Api-Version"] = "2022-11-28"
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                timeout = max(0.1, min(self.config.timeout_seconds, self.deadline - self.clock()))
                with self._opener.open(request, timeout=timeout) as response:
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                    response_headers = dict(response.headers.items())
                if github:
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
                retry_after = error.headers.get("Retry-After")
                remaining = error.headers.get("X-RateLimit-Remaining")
                # Conservatively back off on 403: secondary limits do not always
                # include Retry-After or an exhausted primary quota.
                limited = status == 429 or (status == 403 and (retry_after or remaining == "0"))
                if github and (limited or status == 403):
                    try:
                        delay = float(retry_after or "60")
                    except ValueError:
                        delay = 60
                    try:
                        reset_delay = float(error.headers.get("X-RateLimit-Reset", "0")) - time.time()
                    except ValueError:
                        reset_delay = 0
                    with self.backoff.lock:
                        self.backoff.until = max(self.backoff.until, self.clock() + max(60, delay, reset_delay))
                error.close()
                if limited:
                    service = "GitHub API" if github else "Status API"
                    raise ProbeError("rate_limit", f"{service} rate limited; checks deferred") from None
                if status >= 500 and attempt < 2:
                    self._wait(attempt)
                    continue
                kind = {
                    401: "authentication", 403: "permission_or_secondary_limit",
                    404: "not_found_or_hidden", 408: "timeout", 409: "conflict",
                }.get(status, "service_error")
                raise ProbeError(kind, f"Read-only endpoint returned HTTP {status}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    self._wait(attempt)
                    continue
                raise ProbeError("network", "Network/timeout failure after bounded retries") from None
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
        if not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
            raise ProbeError("invalid_path", "Only fixed GitHub API paths are allowed")
        data, _ = self._json(API_ROOT + path, github=True)
        return data


def repository_path(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise ProbeError("invalid_response", "GitHub returned an invalid repository name")
    return "/repos/" + name


def inventory(client: Client, *, deadline: float | None = None, request_limit: int | None = None) -> dict:
    repositories: list[dict] = []
    seen: set[str] = set()
    page = 1
    reason = None
    while True:
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise ProbeError("inventory_deadline", "Inventory phase time budget exhausted; remaining cycle reserved for incident probes")
            if request_limit is not None and client.requests >= request_limit:
                raise ProbeError("inventory_request_limit", "Inventory phase request budget exhausted; remaining requests reserved for incident probes")
            rows = client.get(
                f"/user/repos?per_page=100&page={page}&affiliation=owner,collaborator,organization_member&sort=full_name&direction=asc"
            )
            if not isinstance(rows, list):
                raise ProbeError("invalid_response", "Repository enumeration was not a list")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("full_name"), str):
                    raise ProbeError("invalid_response", "Invalid repository in enumeration")
                name = row["full_name"]
                repository_path(name)
                if name in seen:
                    continue
                if len(repositories) >= client.config.max_repositories:
                    reason = {"kind": "inventory_limit", "message": "Configured repository inventory limit reached"}
                    break
                seen.add(name)
                repositories.append({
                    "name": name, "url": f"https://github.com/{name}",
                    "archived": bool(row.get("archived")),
                    "has_issues": row.get("has_issues") if isinstance(row.get("has_issues"), bool) else None,
                    "has_pages": row.get("has_pages") if isinstance(row.get("has_pages"), bool) else None,
                })
            if reason or len(rows) < 100:
                break
            page += 1
        except ProbeError as error:
            reason = {"kind": error.kind, "message": error.message}
            break
    if reason is None and getattr(client, "access_warnings", set()):
        reason = {"kind": "sso_partial_results", "message": "SSO restrictions reported by GitHub; enumeration is partial"}
    return {
        "repositories": repositories, "complete": reason is None, "reason": reason,
        "discovered": len(repositories),
        "scope": "Credential-visible owned, collaborator and organization-member repositories. Invisible/SSO-restricted repositories and unrelated public repositories are not enumerable here.",
        "access_caveat": "Enumeration exhausted is not proof of full account access: token selection, SSO and permissions can silently hide repositories.",
    }
