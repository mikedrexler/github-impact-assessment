import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from impact.assessment import assess, probe
from impact.client import Config
from impact.copilot_probe import CopilotProbe, OUTCOMES, PROMPT, SDK_VERSION, sdk_inference, worker_main
from impact.demo import DemoClient
from impact.monitor import Monitor
from tests.test_assessment import FixtureClient, INCIDENT, NOW, REPO

COMPONENT = {"id": "models", "name": "Copilot AI Model Providers", "status": "degraded_performance"}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.options = dict(enabled=True, model="fixture-model", token="copilot-secret",
                            cli_path=sys.executable, ledger=Path(self.temp.name) / "attempts.db")

    def test_disabled_invalid_configuration_and_isolated_mode_never_spawn(self):
        for overrides, expected in [
            ({"enabled": False}, "disabled"), ({"model": "auto"}, "invalid_model"),
            ({"model": "bad/model"}, "invalid_model"), ({"token": ""}, "missing_credential"),
            ({"cli_path": "relative"}, "missing_cli"), ({"isolated": True}, "disabled"),
        ]:
            with self.subTest(expected=expected), patch("impact.copilot_probe.subprocess.Popen") as popen:
                health = CopilotProbe(**(self.options | overrides))
                health.run(["i"])
                self.assertEqual(health.snapshot()["status"], expected)
                popen.assert_not_called()
        monitor = Monitor(Config(), demo=True, client_factory=DemoClient)
        self.assertEqual(monitor.request_copilot_probe(), "not_configured")

    def test_cap_persists_across_instances_failed_requests_count_and_rollback_fails_closed(self):
        for index in range(3):
            health = CopilotProbe(**self.options)
            with patch("impact.copilot_probe.time.time", return_value=10000 + index * 30), patch.object(
                health, "_execute", return_value="timeout",
            ) as execute:
                health.run(["i"])
                execute.assert_called_once()
        health = CopilotProbe(**self.options)
        with patch("impact.copilot_probe.time.time", return_value=10100), patch.object(health, "_execute") as execute:
            health.run(["i"])
            self.assertEqual(health.snapshot()["status"], "hourly_limit")
            execute.assert_not_called()
        with patch("impact.copilot_probe.time.time", return_value=9000):
            health.run(["i"])
            self.assertEqual(health.snapshot()["status"], "already_running_or_cooling_down")
        with patch("impact.copilot_probe.time.time", return_value=13661), patch.object(health, "_execute", return_value="success"):
            health.run(["i"])
            self.assertEqual(health.snapshot()["sample"]["outcome"], "success")

    def test_shared_lease_and_storage_errors_never_send(self):
        first = CopilotProbe(**self.options)
        second = CopilotProbe(**self.options)
        with patch("impact.copilot_probe.time.time", return_value=10000), patch.object(first, "_execute", return_value="success"):
            first.run(["i"])
        with patch("impact.copilot_probe.time.time", return_value=10001), patch.object(second, "_execute") as execute:
            second.run(["i"])
            execute.assert_not_called()
        with patch.object(second, "_reserve", side_effect=sqlite3.OperationalError("secret")), patch.object(second, "_execute") as execute:
            second.run(["i"])
            self.assertEqual(second.snapshot()["status"], "budget_storage_error")
            execute.assert_not_called()
        self.assertNotIn("secret", json.dumps(second.snapshot()))

    def test_single_flight_and_public_sample_contains_no_credentials_or_transcript(self):
        health = CopilotProbe(**self.options)
        entered, release = threading.Event(), threading.Event()

        def execute():
            entered.set()
            release.wait(2)
            return "success"

        with patch.object(health, "_execute", side_effect=execute) as call:
            thread = threading.Thread(target=health.run, args=(["i"],))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                health.run(["i"])
                self.assertEqual(health.snapshot()["status"], "running")
            finally:
                release.set()
                thread.join(2)
            self.assertEqual(call.call_count, 1)
        public = json.dumps(health.snapshot())
        self.assertNotIn("copilot-secret", public)
        self.assertNotIn(self.temp.name, public)
        self.assertIsNone(health.snapshot()["sample"]["actual_model"])
        with contextlib.closing(sqlite3.connect(self.options["ledger"])) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_subprocess_isolation_timeout_and_output_allowlist(self):
        health = CopilotProbe(**self.options)
        for output, expected in [
            (b'{"outcome":"success"}', "success"),
            (b'{"outcome":"run shell"}', "runtime_error"),
            (b'{"outcome":"success","token":"secret"}', "runtime_error"),
            (b"x" * 1025, "runtime_error"), (b"[]", "runtime_error"),
            (b'{"outcome":[]}', "runtime_error"),
        ]:
            with self.subTest(output=output), patch("impact.copilot_probe.os.killpg") as kill:
                def popen(command, **kwargs):
                    self.assertNotIn("shell", kwargs)
                    self.assertTrue(kwargs["start_new_session"])
                    self.assertEqual(command[0], sys.executable)
                    env = kwargs["env"]
                    self.assertNotIn("GH_TOKEN", env)
                    self.assertNotIn("OPENAI_API_KEY", env)
                    self.assertEqual(env["HOME"], kwargs["cwd"])
                    self.assertEqual(env["COPILOT_SKIP_CLI_DOWNLOAD"], "1")
                    self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
                    kwargs["stdout"].write(output)
                    return SimpleNamespace(pid=1234567, returncode=0, wait=Mock())
                with patch("impact.copilot_probe.subprocess.Popen", side_effect=popen):
                    self.assertEqual(health._execute(), expected)
                kill.assert_called_once()
        process = SimpleNamespace(pid=1234567, wait=Mock(side_effect=[subprocess.TimeoutExpired("worker", 15), None]))
        with patch("impact.copilot_probe.subprocess.Popen", return_value=process), patch("impact.copilot_probe.os.killpg") as kill:
            self.assertEqual(health._execute(), "timeout")
            kill.assert_called_once()
            self.assertEqual(process.wait.call_args_list[0].kwargs["timeout"], 15)

    def test_worker_missing_wrong_sdk_and_errors_emit_only_sanitized_enum(self):
        for version, expected in [("older", "unsupported_sdk"), (None, "sdk_unavailable")]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch(
                "impact.copilot_probe.importlib.metadata.version",
                side_effect=__import__("importlib.metadata").metadata.PackageNotFoundError() if version is None else None,
                return_value=version,
            ):
                worker_main()
            self.assertEqual(json.loads(output.getvalue()), {"outcome": expected})
        with patch("impact.copilot_probe.importlib.metadata.version", return_value=SDK_VERSION), patch(
            "impact.copilot_probe.importlib.import_module", side_effect=RuntimeError("secret-token"),
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            worker_main()
        self.assertEqual(json.loads(output.getvalue()), {"outcome": "client_error"})
        self.assertNotIn("secret-token", output.getvalue())

    def test_worker_with_unsupported_sdk_makes_no_inference(self):
        health = CopilotProbe(**self.options)
        # Never launch an installed SDK during offline tests.
        with patch("impact.copilot_probe.importlib.metadata.version", return_value="not-installed"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                worker_main()
        self.assertEqual(json.loads(output.getvalue())["outcome"], "unsupported_sdk")

    def test_scheduler_manual_only_deduplicates_and_rechecks_incident(self):
        health = CopilotProbe(**self.options)
        class ModelsDemo(DemoClient):
            def status(self):
                return {"components": [COMPONENT], "incidents": [{**INCIDENT, "components": [{"id": "models"}]}]}
        monitor = Monitor(Config(), client_factory=ModelsDemo, copilot_probe=health)
        with patch.object(health, "_execute", return_value="success") as execute:
            monitor._cycle()
            execute.assert_not_called()
            self.assertEqual(monitor.request_copilot_probe(), "accepted")
            self.assertEqual(monitor.request_copilot_probe(), "already_running")
            monitor._cycle()
            execute.assert_called_once()
            self.assertTrue(all(row["classification"] == "unknown" for row in monitor.snapshot()["latest"]["checks"]))
            self.assertTrue(all(row["evidence"] for row in monitor.snapshot()["latest"]["checks"]))
            monitor._cycle()
            execute.assert_called_once()
        with patch.object(health, "run") as run:
            client = ModelsDemo(Config(), threading.Event())
            client.status = lambda: {"components": [COMPONENT], "incidents": []}
            assess(client, copilot_probe=health, run_copilot=True)
            run.assert_not_called()

    def test_success_and_all_failures_remain_unknown_no_network_per_repository(self):
        for outcome in OUTCOMES:
            sample = {
                "outcome": outcome, "checked_at": NOW.isoformat(), "incident_ids": [INCIDENT["id"]],
                "requested_model": "fixture-model", "latency_ms": 12,
            }
            client = FixtureClient()
            row = probe(client, REPO, COMPONENT, INCIDENT, NOW,
                        {"configuration": "ready", "status": "completed", "sample": sample})
            self.assertEqual(row["classification"], "unknown")
            self.assertEqual(len(row["evidence"]), 1)
            self.assertIn("Same sample reused", row["evidence"][0]["summary"])
            self.assertEqual(client.calls, [])
            if outcome == "success":
                self.assertIn("succeeded", row["reason"])
            old = probe(client, REPO, COMPONENT, INCIDENT, NOW + timedelta(minutes=6),
                        {"configuration": "ready", "status": "completed", "sample": sample})
            self.assertIn("STALE", old["evidence"][0]["summary"])
            mismatch = probe(client, REPO, COMPONENT, {**INCIDENT, "id": "different"}, NOW,
                             {"configuration": "ready", "status": "completed", "sample": sample})
            self.assertEqual(mismatch["evidence"], [])


class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_exact_model_no_tools_no_configuration_no_repo_context_and_cleanup(self):
        session = SimpleNamespace(send_and_wait=AsyncMock(return_value=SimpleNamespace(data=SimpleNamespace(content="IMPACT_PROBE_OK"))))
        client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
                                 list_models=AsyncMock(return_value=[SimpleNamespace(id="fixture-model")]),
                                 create_session=AsyncMock(return_value=session))
        sdk = SimpleNamespace(CopilotClient=Mock(return_value=client), RuntimeConnection=SimpleNamespace(for_stdio=Mock()))
        with patch("impact.copilot_probe.importlib.import_module", return_value=SimpleNamespace(PermissionDecisionReject=Mock(return_value="denied"))):
            result = await sdk_inference(sdk, "secret", "fixture-model", "/trusted/copilot", "/isolated")
            self.assertEqual(result, "success")
            options = client.create_session.call_args.kwargs
            for key in ["available_tools", "tools", "custom_agents", "skill_directories", "plugin_directories"]:
                self.assertEqual(options[key], [])
            self.assertEqual(options["mcp_servers"], {})
            for key in ["enable_config_discovery", "enable_file_hooks", "enable_host_git_operations", "enable_session_store", "enable_skills", "enable_session_telemetry"]:
                self.assertFalse(options[key])
            self.assertEqual(options["on_permission_request"]({"kind": "write"}, {}), "denied")
            self.assertEqual(sdk.CopilotClient.call_args.kwargs["mode"], "empty")
            self.assertFalse(sdk.CopilotClient.call_args.kwargs["use_logged_in_user"])
            self.assertEqual(sdk.RuntimeConnection.for_stdio.call_args.kwargs["args"],
                             ["--disable-client-telemetry", "--no-remote-export"])
            self.assertTrue(options["enable_managed_settings"])
            self.assertTrue(options["enforce_managed_model_defaults"])
            self.assertEqual(options["github_token"], "secret")
            session.send_and_wait.assert_awaited_once_with(PROMPT, timeout=13)
            client.stop.assert_awaited_once()

    async def test_unavailable_model_and_exception_never_retry_and_always_stop(self):
        client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
                                 list_models=AsyncMock(return_value=[]), create_session=AsyncMock())
        sdk = SimpleNamespace(CopilotClient=Mock(return_value=client), RuntimeConnection=SimpleNamespace(for_stdio=Mock()))
        self.assertEqual(await sdk_inference(sdk, "secret", "missing", "/cli", "/tmp"), "model_unavailable")
        client.create_session.assert_not_called()
        client.stop.assert_awaited_once()
        client.list_models.side_effect = RuntimeError("secret")
        with self.assertRaises(RuntimeError):
            await sdk_inference(sdk, "secret", "missing", "/cli", "/tmp")
        self.assertEqual(client.list_models.await_count, 2)
        self.assertEqual(client.stop.await_count, 2)
