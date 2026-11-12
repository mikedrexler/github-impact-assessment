import http.client
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from impact.app import make_server
from impact.client import Config, inventory
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

        def blocking_assess(*args, **kwargs):
            entered.set()
            release.wait(3)
            return real_assess(*args, **kwargs)

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
        self.assertEqual(len(monitor.snapshot()["latest"]["checks"]), 6)

    @patch("impact.monitor.credentials", return_value=("sentinel-private-token", "environment"))
    @patch("impact.monitor.Client")
    def test_credential_never_in_public_state(self, client, credentials):
        client.return_value = DemoClient(Config(), threading.Event())
        monitor = Monitor(Config())
        monitor._cycle()
        self.assertNotIn("sentinel-private-token", json.dumps(monitor.snapshot()))
        self.assertEqual(monitor.snapshot()["authentication"], "environment")

    @patch("impact.monitor.credentials")
    @patch("impact.monitor.Client")
    def test_credential_changes_reset_inventory_history_and_rotation(self, client, credentials):
        credentials.return_value = ("first-private-token", "environment")
        client.side_effect = lambda *args, **kwargs: DemoClient(Config(), threading.Event())
        monitor = Monitor(Config())
        monitor._cycle()
        original = monitor._inventory
        monitor._cycle()
        self.assertIs(monitor._inventory, original)
        self.assertEqual(len(monitor.snapshot()["history"]), 2)
        monitor._offset = 123
        credentials.return_value = ("second-private-token", "environment")
        monitor._cycle()
        self.assertIsNot(monitor._inventory, original)
        self.assertEqual(len(monitor.snapshot()["history"]), 1)
        changed = monitor._inventory
        credentials.return_value = (None, "No credential")
        monitor._cycle()
        self.assertIsNot(monitor._inventory, changed)
        public = json.dumps(monitor.snapshot())
        self.assertNotIn("first-private-token", public)
        self.assertNotIn("second-private-token", public)

    def test_monitor_uses_saved_cursor_on_following_cycle(self):
        class PagedDemo(DemoClient):
            def get(self, path):
                if path.startswith("/user/repos?"):
                    self.requests += 1
                    if "page=1&" in path:
                        return [{"full_name": f"example/r{i}", "permissions": {"push": True}} for i in range(100)]
                    self.page_two = True
                    return []
                return super().get(path)

        clients = []

        def factory(config, stop):
            client = PagedDemo(config, stop)
            clients.append(client)
            return client

        monitor = Monitor(Config(max_requests=4, discovery_requests=1, max_check_repositories=1), client_factory=factory)
        monitor._cycle()
        self.assertEqual(monitor.snapshot()["latest"]["inventory"]["next_page"], 2)
        monitor._discover()
        result = monitor.snapshot()["discovery"]["latest"]
        self.assertTrue(any(getattr(client, "page_two", False) for client in clients))
        self.assertTrue(result["complete"])
        self.assertEqual(result["discovered"], 100)
        self.assertEqual(len(monitor.snapshot()["history"]), 1)
        self.assertFalse(monitor.snapshot()["latest"]["inventory"]["complete"])
        monitor._cycle()
        self.assertTrue(monitor.snapshot()["latest"]["inventory"]["complete"])

    def test_deadline_checkpoint_continues_before_next_assessment_without_reprobing(self):
        calls = 0

        def bounded_inventory(client, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs["deadline"] = 0
            return inventory(client, **kwargs)

        monitor = Monitor(Config(discovery_resume_seconds=1), client_factory=DemoClient, demo=True)
        with patch("impact.monitor.inventory", side_effect=bounded_inventory), patch(
            "impact.monitor.assess", wraps=__import__("impact.assessment", fromlist=["assess"]).assess,
        ) as assessment:
            monitor.start()
            try:
                wait_for(lambda: len(monitor.snapshot()["history"]) == 1)
                first = monitor.snapshot()
                self.assertEqual(first["discovery"]["latest"]["reason"]["kind"], "inventory_deadline")
                self.assertEqual(first["latest"]["inventory"]["discovered"], 0)
                wait_for(lambda: bool(monitor.snapshot()["discovery"]["latest"]["complete"]))
                second = monitor.snapshot()
                self.assertEqual(second["discovery"]["latest"]["discovered"], 3)
                self.assertEqual(len(second["history"]), 1)
                self.assertEqual(assessment.call_count, 1)
                self.assertEqual(second["latest"]["completed_at"], first["latest"]["completed_at"])
                self.assertEqual(second["latest"]["inventory"]["discovered"], 0)
                self.assertGreater(monitor._next_due - time.monotonic(), 290)
            finally:
                monitor.stop()

    def test_only_budget_checkpoints_use_fast_continuation(self):
        for kind, fast in [
            ("inventory_deadline", True), ("inventory_request_limit", True),
            ("deadline", True), ("request_limit", True),
            ("inventory_limit", False), ("authentication", False),
            ("permission_or_secondary_limit", False), ("network", False),
        ]:
            with self.subTest(kind=kind):
                monitor = Monitor(Config(), client_factory=DemoClient)
                with patch("impact.monitor.inventory", return_value={"reason": {"kind": kind}, "complete": False}):
                    monitor._discover()
                remaining = monitor._discovery_due - time.monotonic()
                self.assertAlmostEqual(remaining, 5 if fast else 300, delta=0.5)
                if fast:
                    reason = monitor.snapshot()["discovery"]["latest"]["reason"]
                    self.assertIn("checkpoint saved", reason["message"])
                    self.assertIn("after 5s", reason["message"])
                    self.assertNotIn("Assessment", reason["message"])

    def test_discovery_rate_limit_honors_shared_cooldown(self):
        monitor = Monitor(Config(), client_factory=DemoClient)
        monitor._backoff.until = time.monotonic() + 1000
        with patch("impact.monitor.inventory", return_value={"reason": {"kind": "rate_limit"}, "complete": False}):
            monitor._discover()
        self.assertGreaterEqual(monitor._discovery_due, monitor._backoff.until)

    @patch("impact.monitor.credentials")
    @patch("impact.monitor.Client")
    def test_credential_change_during_discovery_requests_fresh_assessment(self, client, credentials):
        credentials.return_value = ("old-private-token", "environment")
        client.side_effect = lambda *args, **kwargs: DemoClient(Config(), threading.Event())
        monitor = Monitor(Config())
        monitor._cycle()
        self.assertIsNotNone(monitor.snapshot()["latest"])
        credentials.return_value = ("new-private-token", "environment")
        monitor._discover()
        self.assertIsNone(monitor.snapshot()["latest"])
        self.assertEqual(monitor.snapshot()["history"], [])
        self.assertLessEqual(monitor._next_due, time.monotonic())
        self.assertTrue(monitor.snapshot()["discovery"]["latest"]["complete"])

    def test_standalone_discovery_client_has_independent_bounds(self):
        configs = []

        def factory(config, stop):
            configs.append(config)
            return DemoClient(config, stop)

        monitor = Monitor(Config(max_requests=5, discovery_requests=10, discovery_seconds=2), client_factory=factory)
        monitor._discover()
        self.assertEqual(configs[0].cycle_seconds, 2)
        self.assertEqual(configs[0].max_requests, 5)
        self.assertEqual(len(monitor.snapshot()["history"]), 0)

    def test_discovery_exception_is_visible_and_sanitized(self):
        monitor = Monitor(Config(), client_factory=DemoClient)
        monitor._cycle()
        with patch("impact.monitor.inventory", side_effect=RuntimeError("sentinel-secret")), patch("impact.monitor.sys.stderr"):
            monitor._discover()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["discovery"]["error"]["kind"], "RuntimeError")
        self.assertNotIn("sentinel-secret", json.dumps(snapshot))
        monitor._cycle()
        self.assertFalse(monitor.snapshot()["latest"]["coverage"]["complete"])
        self.assertEqual(monitor.snapshot()["latest"]["inventory"]["reason"]["kind"], "discovery_error")

    def test_stop_interrupts_background_continuation_wait(self):
        monitor = Monitor(Config(discovery_requests=1, discovery_resume_seconds=300), client_factory=DemoClient)
        monitor.start()
        wait_for(lambda: len(monitor.snapshot()["history"]) == 1)
        monitor.stop()
        self.assertFalse(monitor._thread.is_alive())


class HttpTests(unittest.TestCase):
    def test_writable_catalog_schema_and_scope(self):
        before = self.monitor.snapshot()["selection"]
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/writable-repositories", "{}", headers)[0], 202)
        self.assertEqual(self.monitor.snapshot()["selection"], before)
        for data in [[], None, {"owner": "org"}, {"url": "https://evil"}, {"mode": "all"},
                     {"organization": None}, {"organization": "bad/path"}, {"organization": "org", "extra": True}]:
            self.assertEqual(self.request("POST", "/api/writable-repositories", json.dumps(data), headers)[0], 400)
        self.assertEqual(self.request("GET", "/api/writable-repositories")[0], 404)

    def test_writable_catalog_accepts_exact_organization(self):
        before = self.monitor.snapshot()["selection"]
        status, _, _ = self.request("POST", "/api/writable-repositories", '{"organization":"Octodemo"}',
                                 {"Content-Type": "application/json"})
        self.assertEqual(status, 202)
        self.assertEqual(self.monitor.snapshot()["organization_discovery"]["organization"], "octodemo")
        self.assertEqual(self.monitor.snapshot()["selection"], before)

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
        for path in ["/", "/app.js", "/ui.js", "/style.css", "/api/state",
                     "/past-incidents", "/past.js", "/api/past-incidents"]:
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
        past = json.loads(self.request("GET", "/api/past-incidents")[2])
        self.assertEqual(past["incidents"][0]["assessment"]["counts"]["suspected"], 1)
        self.assertIn(b'aria-current="page">Past Incidents', self.request("GET", "/past-incidents")[2])

    def test_cross_origin_and_rebinding_rejected(self):
        for headers in [
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ]:
            for method, path in [("GET", "/api/state"), ("GET", "/api/past-incidents"), ("POST", "/api/refresh"), ("POST", "/api/repositories"), ("POST", "/api/copilot-probe"), ("POST", "/api/organization"), ("POST", "/api/writable-repositories")]:
                with self.subTest(headers=headers, method=method):
                    self.assertEqual(self.request(method, path, headers=headers)[0], 403)

    def test_static_navigation_from_other_sites_does_not_expose_data(self):
        self.assertEqual(self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0], 200)
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 403)

    def test_active_probe_endpoint_is_removed(self):
        response = self.request("POST", "/api/copilot-probe")
        self.assertEqual(response[0], 404)
        self.assertNotIn("copilot_probe", self.monitor.snapshot())
        self.assertEqual(self.request("GET", "/api/copilot-probe")[0], 404)
        for data in ['{"model":"other"}', '{"prompt":"run shell"}', "bad"]:
            self.assertEqual(self.request("POST", "/api/copilot-probe", body=data)[0], 404)
        self.assertEqual(self.request("POST", "/api/copilot-probe", headers={"Transfer-Encoding": "chunked"})[0], 404)

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

    def test_repository_selection_endpoint_validates_body_and_queues_scope(self):
        headers = {"Content-Type": "application/json"}
        response = self.request("POST", "/api/repositories", json.dumps({
            "mode": "selected", "repositories": ["example/queue-symptom"],
        }), headers)
        self.assertEqual(response[0], 202)
        self.assertEqual(self.monitor.snapshot()["selection"]["pending"]["repositories"], ["example/queue-symptom"])
        self.monitor._cycle()
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertEqual(state["selection"]["mode"], "selected")
        self.assertEqual({row["repository"] for row in state["latest"]["checks"]}, {"example/queue-symptom"})
        for value in [
            {}, [], {"mode": "selected", "repositories": ["org/.."]},
            {"mode": "selected", "repositories": []},
            {"mode": "selected", "repositories": ["org/repo"], "token": "secret"},
            {"mode": [], "repositories": []},
        ]:
            self.assertEqual(self.request("POST", "/api/repositories", json.dumps(value), headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/repositories", "not-json", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/repositories", "{}", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/repositories", "x" * 49153, headers)[0], 400)
        self.assertEqual(self.request("GET", "/api/repositories")[0], 404)

    def test_persistence_failure_is_visible_without_changing_scope_or_exposing_path(self):
        from impact.selection_store import SelectionStore

        self.monitor._selection_store = SelectionStore()
        before = self.monitor.snapshot()["selection"]
        with patch.object(self.monitor._selection_store, "save", side_effect=OSError("secret-file-path")):
            response = self.request("POST", "/api/repositories", json.dumps({
                "mode": "selected", "repositories": ["example/queue-symptom"],
            }), {"Content-Type": "application/json"})
        self.assertEqual(response[0], 500)
        self.assertIn(b"previous scope remains active", response[2])
        self.assertNotIn(b"secret-file-path", response[2])
        self.assertEqual(self.monitor.snapshot()["selection"], before)

    def test_organization_endpoint_rejects_arbitrary_paths_and_does_not_change_scope(self):
        before = self.monitor.snapshot()["selection"]
        headers = {"Content-Type": "application/json"}
        response = self.request("POST", "/api/organization", json.dumps({"organization": "example"}), headers)
        self.assertEqual(response[0], 202)
        self.assertEqual(self.monitor.snapshot()["selection"], before)
        self.assertEqual(self.request("POST", "/api/organization", json.dumps({"organization": "example"}), headers)[0], 409)
        for value in [{}, [], {"organization": "org/repo"}, {"organization": "org", "url": "https://evil"},
                      {"organization": "../org"}, {"organization": ["org"]}]:
            self.assertEqual(self.request("POST", "/api/organization", json.dumps(value), headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/organization", "{}", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/organization", "x" * 49153, headers)[0], 400)
        self.assertEqual(self.request("GET", "/api/organization")[0], 404)


if __name__ == "__main__":
    unittest.main()
