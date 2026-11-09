import http.client
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from impact.app import make_server
from impact.client import Config
from impact.demo import DemoClient
from impact.monitor import Monitor


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for test state")


class MonitorTests(unittest.TestCase):
    def test_periodic_polling_runs_without_manual_refresh(self):
        monitor = Monitor(Config(poll_seconds=1), client_factory=DemoClient, demo=True)
        monitor.start()
        try:
            wait_for(lambda: len(monitor.snapshot()["history"]) >= 2)
        finally:
            monitor.stop()
        self.assertFalse(monitor._thread.is_alive())

    def test_scheduler_deduplicates_and_stops(self):
        entered = threading.Event()
        release = threading.Event()
        real_assess = __import__("impact.assessment", fromlist=["assess"]).assess

        def blocking_assess(*args):
            entered.set()
            release.wait(3)
            return real_assess(*args)

        monitor = Monitor(Config(), client_factory=DemoClient, demo=True)
        with patch("impact.monitor.assess", side_effect=blocking_assess) as assessment:
            monitor.start()
            try:
                self.assertTrue(entered.wait(2))
                for _ in range(20):
                    self.assertEqual(monitor.refresh(), "already_running")
                release.set()
                wait_for(lambda: not monitor.snapshot()["busy"])
                self.assertEqual(assessment.call_count, 1)
                self.assertEqual(len(monitor.snapshot()["history"]), 1)
                self.assertEqual(monitor.refresh(), "accepted")
                wait_for(lambda: len(monitor.snapshot()["history"]) == 2)
                self.assertEqual(assessment.call_count, 2)
                self.assertEqual(monitor.refresh(), "throttled")
            finally:
                release.set()
                monitor.stop()
        self.assertEqual(monitor.refresh(), "stopped")
        self.assertFalse(monitor._thread.is_alive())

    def test_unexpected_error_is_visible_but_raw_exception_is_not(self):
        monitor = Monitor(Config(), client_factory=DemoClient)
        with patch("impact.monitor.assess", side_effect=RuntimeError("sentinel-secret-response")), patch("impact.monitor.sys.stderr"):
            monitor._cycle()
        state = monitor.snapshot()
        self.assertEqual(state["error"]["kind"], "RuntimeError")
        self.assertNotIn("sentinel-secret-response", json.dumps(state))
        self.assertFalse(state["busy"])

    def test_history_bounded_and_snapshot_is_copy(self):
        monitor = Monitor(Config(), client_factory=DemoClient, demo=True)
        for _ in range(12):
            monitor._cycle()
        self.assertEqual(len(monitor.snapshot()["history"]), 10)
        snapshot = monitor.snapshot()
        snapshot["latest"]["checks"].clear()
        self.assertEqual(len(monitor.snapshot()["latest"]["checks"]), 9)

    @patch("impact.monitor.credentials", return_value=("sentinel-private-token", "environment"))
    @patch("impact.monitor.Client")
    def test_credential_never_in_public_state(self, client, credentials):
        client.return_value = DemoClient(Config(), threading.Event())
        monitor = Monitor(Config())
        monitor._cycle()
        self.assertNotIn("sentinel-private-token", json.dumps(monitor.snapshot()))
        self.assertEqual(monitor.snapshot()["authentication"], "environment")


class HttpTests(unittest.TestCase):
    def setUp(self):
        # Port zero is used only by tests, not accepted in user environment config.
        self.monitor = Monitor(Config(port=0), client_factory=DemoClient, demo=True)
        self.monitor._cycle()
        self.server = make_server(self.monitor)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.monitor.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        status, response_headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, data

    def test_dashboard_state_assets_and_privacy_headers(self):
        for path in ["/", "/app.js", "/style.css", "/api/state"]:
            with self.subTest(path=path):
                status, headers, data = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertNotIn(b"Bearer ", data)
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertTrue(state["demo"])
        self.assertEqual(state["latest"]["counts"]["suspected"], 1)

    def test_cross_origin_and_rebinding_rejected(self):
        for headers in [
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ]:
            for method, path in [("GET", "/api/state"), ("POST", "/api/refresh")]:
                with self.subTest(headers=headers, method=method):
                    self.assertEqual(self.request(method, path, headers=headers)[0], 403)

    def test_static_navigation_from_other_sites_does_not_expose_data(self):
        self.assertEqual(self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0], 200)
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 403)

    def test_local_refresh_only_and_no_arbitrary_routes(self):
        self.assertEqual(self.request("POST", "/api/refresh")[0], 202)
        self.assertEqual(self.request("POST", "/api/refresh")[0], 409)
        self.assertEqual(self.request("POST", "/api/refresh", body="bad")[0], 400)
        for path in ["/api/token", "/.env", "/../client.py", "/api/refresh"]:
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/repos/example/repo/dispatches")[0], 404)

    def test_bind_failure_has_nonzero_module_exit(self):
        import os
        environment = {**os.environ, "IMPACT_PORT": str(self.port)}
        response = subprocess.run(
            [sys.executable, "-m", "impact", "--demo"],
            env=environment, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(response.returncode, 1)
        self.assertIn("Cannot bind", response.stderr)


if __name__ == "__main__":
    unittest.main()
