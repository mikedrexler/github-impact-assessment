"""Opt-in active inference, separate from the read-only GitHub evidence client."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

SDK_VERSION = "1.0.29"
PROMPT = "Reply with exactly IMPACT_PROBE_OK. Do not use any tools."
TIMEOUT = 15
FRESH_SECONDS = 300
OUTCOMES = {
    "success", "unexpected_response", "client_error", "model_unavailable",
    "sdk_unavailable", "unsupported_sdk", "timeout", "runtime_error",
}
LIMITATION = (
    "Shared active Copilot SDK client/model-path sample, not a repository or IDE test. "
    "The configured credential's enterprise/tenant membership is not verified. "
    "Success is point-in-time only; errors may be authentication, policy, quota, "
    "local network or provider failures. Neither establishes repository impact."
)


def valid_model(value: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}", value)) and value.casefold() != "auto"


class CopilotProbe:
    def __init__(self, *, enabled: bool = False, model: str = "", token: str = "",
                 cli_path: str = "", ledger: Path | None = None, isolated: bool = False):
        self._model = model
        self._token = token
        self._cli_path = cli_path
        self._ledger = ledger or Path.home() / ".local/state/github-impact-assessment/copilot-probe.sqlite3"
        self._lock = threading.Lock()
        self._sample: dict | None = None
        self._status = "idle"
        self._running = False
        self._enabled = enabled and not isolated
        self._configuration = (
            "disabled" if not self._enabled else
            "invalid_model" if not valid_model(model) else
            "missing_credential" if not token else
            "missing_cli" if not Path(cli_path).is_absolute() or not os.access(cli_path, os.X_OK) else
            "unsupported_platform" if os.name != "posix" else "ready"
        )

    @classmethod
    def from_env(cls, *, isolated: bool = False) -> CopilotProbe:
        return cls(
            enabled=os.environ.get("IMPACT_COPILOT_PROBE_ENABLED") == "1",
            model=os.environ.get("IMPACT_COPILOT_PROBE_MODEL", ""),
            token=os.environ.get("IMPACT_COPILOT_PROBE_TOKEN", ""),
            cli_path=os.environ.get("IMPACT_COPILOT_CLI_PATH", ""),
            isolated=isolated,
        )

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "configuration": self._configuration, "status": self._status,
                "requested_model": self._model if valid_model(self._model) else None,
                "sample": self._sample, "limitations": LIMITATION,
                "limits": {"attempts_per_hour": 3, "timeout_seconds": TIMEOUT,
                           "fresh_seconds": FRESH_SECONDS, "manual_only": True},
            })

    def skip(self) -> None:
        with self._lock:
            self._status = "skipped_no_scoped_model_incident_or_stopped"

    def _reserve(self, now: float) -> str:
        # A committed reservation counts even when the process crashes/times out.
        # BEGIN IMMEDIATE shares both the cap and the short lease across servers.
        self._ledger.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self._ledger, timeout=1)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (at REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            rows = [row[0] for row in db.execute("SELECT at FROM attempts ORDER BY at")]
            if rows and (rows[-1] > now or now - rows[-1] < TIMEOUT + 5):
                return "already_running_or_cooling_down"
            recent = [at for at in rows if at > now - 3600]
            if len(recent) >= 3:
                return "hourly_limit"
            db.execute("DELETE FROM attempts WHERE at <= ?", (now - 3600,))
            db.execute("INSERT INTO attempts VALUES (?)", (now,))
        return "reserved"

    def run(self, incident_ids: list[str]) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._running:
                return
            if self._configuration != "ready":
                self._status = self._configuration
                return
            try:
                reservation = self._reserve(time.time())
            except (OSError, sqlite3.Error):
                self._status = "budget_storage_error"
                return
            if reservation != "reserved":
                self._status = reservation
                return
            self._status = "running"
            self._running = True
        finally:
            self._lock.release()
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        outcome = self._execute()
        with self._lock:
            self._running = False
            self._status = "completed"
            self._sample = {
                "outcome": outcome, "started_at": started_at,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "requested_model": self._model, "actual_model": None,
                "incident_ids": list(incident_ids),
                "scope": "shared-client-path",
            }

    def _execute(self) -> str:
        try:
            with tempfile.TemporaryDirectory(prefix="impact-copilot-") as directory:
                env = {
                    "PATH": os.environ.get("PATH", os.defpath), "HOME": directory, "TMPDIR": directory,
                    "COPILOT_SKIP_CLI_DOWNLOAD": "1",
                    "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
                    "IMPACT_COPILOT_PROBE_TOKEN": self._token,
                    "IMPACT_COPILOT_PROBE_MODEL": self._model,
                    "IMPACT_COPILOT_CLI_PATH": self._cli_path,
                }
                # The worker owns an isolated process group including its SDK CLI.
                # No shell, inherited repository cwd, GH_TOKEN or OpenAI credential.
                with tempfile.TemporaryFile() as output:
                    process = subprocess.Popen(
                        [sys.executable, "-c", "from impact.copilot_probe import worker_main; worker_main()"],
                        cwd=directory, env=env, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.DEVNULL, start_new_session=True,
                    )
                    try:
                        process.wait(timeout=TIMEOUT)
                    except subprocess.TimeoutExpired:
                        return "timeout"
                    finally:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                    output.seek(0)
                    result = output.read(1025)
                    if process.returncode != 0 or len(result) > 1024:
                        return "runtime_error"
                    data = json.loads(result)
                    if not isinstance(data, dict) or set(data) != {"outcome"} or data["outcome"] not in OUTCOMES:
                        return "runtime_error"
                    return data["outcome"]
        except (OSError, ValueError, TypeError, RecursionError):
            return "runtime_error"


async def sdk_inference(sdk, token: str, model: str, cli_path: str, directory: str) -> str:
    """Exactly one send; never discover/fall back to a different requested model."""
    client = sdk.CopilotClient(
        mode="empty", connection=sdk.RuntimeConnection.for_stdio(
            path=cli_path, args=["--disable-client-telemetry", "--no-remote-export"],
        ),
        github_token=token, use_logged_in_user=False, base_directory=directory,
        working_directory=directory, builtin_plugin_directories=[], log_level="none",
    )
    try:
        await client.start()
        models = await client.list_models()
        if not any(item.id == model for item in models):
            return "model_unavailable"
        rpc = importlib.import_module("copilot.generated.rpc")

        def deny_permission(*args, **kwargs):
            return rpc.PermissionDecisionReject()

        session = await client.create_session(
            model=model, available_tools=[], tools=[], mcp_servers={},
            custom_agents=[], on_permission_request=deny_permission,
            working_directory=directory, config_directory=directory,
            enable_config_discovery=False, skip_custom_instructions=True,
            enable_on_demand_instruction_discovery=False,
            enable_file_hooks=False, enable_host_git_operations=False,
            enable_session_store=False, enable_skills=False,
            skill_directories=[], agent_directories=[], hook_directories=[],
            plugin_directories=[], instruction_directories=[],
            enable_session_telemetry=False, memory={"enabled": False},
            infinite_sessions={"enabled": False},
            github_token=token, enable_managed_settings=True,
            enforce_managed_model_defaults=True,
            system_message={"mode": "replace", "content": "Answer the fixed diagnostic prompt only."},
        )
        response = await session.send_and_wait(PROMPT, timeout=TIMEOUT - 2)
        return "success" if response and response.data.content.strip() == "IMPACT_PROBE_OK" else "unexpected_response"
    finally:
        await client.stop()


def worker_main():
    outcome = "client_error"
    # Never relay SDK logs, response text, credentials, exception strings or
    # transport diagnostics. The parent accepts only a fixed outcome enum.
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        try:
            if importlib.metadata.version("github-copilot-sdk") != SDK_VERSION:
                outcome = "unsupported_sdk"
            else:
                sdk = importlib.import_module("copilot")
                outcome = asyncio.run(sdk_inference(
                    sdk, os.environ.pop("IMPACT_COPILOT_PROBE_TOKEN"),
                    os.environ["IMPACT_COPILOT_PROBE_MODEL"],
                    os.environ["IMPACT_COPILOT_CLI_PATH"], os.getcwd(),
                ))
        except (ModuleNotFoundError, importlib.metadata.PackageNotFoundError):
            outcome = "sdk_unavailable"
        except TimeoutError:
            outcome = "timeout"
        except Exception:
            # Foreign SDK boundary: failures stay visible but never expose raw
            # messages (which can include authentication material).
            outcome = "client_error"
    print(json.dumps({"outcome": outcome}))
