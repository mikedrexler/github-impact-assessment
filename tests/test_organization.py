import time
import unittest
from unittest.mock import Mock, patch

from impact.client import Config, InventoryState, ProbeError, inventory, organization_name
from impact.demo import DemoClient
from impact.monitor import Monitor
from tests.test_assessment import FixtureClient
from tests.test_monitor_http import wait_for


class OrganizationTests(unittest.TestCase):
    def test_exact_organization_validation(self):
        self.assertEqual(organization_name("My-Org"), "my-org")
        for value in ["", "-org", "org-", "org/repo", "../org", "org?x=1", "https://github.com/org",
                      "org\n", "a" * 40, None, [], True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                organization_name(value)

    def test_only_chosen_organization_endpoint_and_scope_identity_check(self):
        client = FixtureClient({"/orgs/my-org/repos?": [{"full_name": "my-org/repo"}]})
        report = inventory(client, organization="My-Org")
        self.assertTrue(report["complete"])
        self.assertEqual([repo["name"] for repo in report["repositories"]], ["my-org/repo"])
        self.assertTrue(all(path.startswith("/orgs/my-org/repos?") for path in client.calls))
        with self.assertRaises(ValueError):
            inventory(client, organization="my-org", selected=("my-org/repo",))
        bad = FixtureClient({"/orgs/my-org/repos?": [{"full_name": "other/repo"}]})
        report = inventory(bad, organization="my-org")
        self.assertFalse(report["complete"])
        self.assertEqual(report["reason"]["kind"], "repository_mismatch")
        self.assertEqual(report["repositories"], [])

    def test_pagination_checkpoints_resume_and_permission_errors_remain_partial(self):
        state = InventoryState()
        client = FixtureClient({"/orgs/org/repos?": [{"full_name": f"org/r{i}"} for i in range(100)]})
        report = inventory(client, organization="org", state=state, request_limit=1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["next_page"], 2)
        client = FixtureClient({"/orgs/org/repos?": []})
        report = inventory(client, organization="org", state=state)
        self.assertIn("page=2&", client.calls[0])
        self.assertTrue(report["complete"])
        self.assertEqual(report["discovered"], 100)
        for kind in ["authentication", "permission_or_secondary_limit", "rate_limit", "not_found_or_hidden"]:
            client = FixtureClient({"/orgs/org/repos?": ProbeError(kind, "Cannot read organization")})
            report = inventory(client, organization="org")
            self.assertFalse(report["complete"])
            self.assertEqual(report["reason"]["kind"], kind)
            self.assertEqual(report["repositories"], [])

    def test_organization_discovery_keeps_assessment_scope_and_saved_selection_unchanged(self):
        calls = []

        class Tracked(DemoClient):
            def get(self, path):
                calls.append(path)
                return super().get(path)

        store = Mock()
        monitor = Monitor(Config(), client_factory=Tracked, selected=(), selection_store=store)
        before = monitor.snapshot()["selection"]
        self.assertEqual(monitor.discover_organization("example"), "accepted")
        self.assertEqual(monitor.discover_organization("example"), "already_running")
        monitor._discover_organization()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["selection"], before)
        self.assertEqual(snapshot["organization_discovery"]["latest"]["discovered"], 3)
        self.assertEqual(len(snapshot["history"]), 0)
        store.save.assert_not_called()
        self.assertTrue(all(path.startswith("/orgs/example/repos?") for path in calls))
        self.assertEqual(monitor.discover_organization("other"), "throttled")
        monitor.stop()
        self.assertEqual(monitor.discover_organization("other"), "stopped")

    def test_scheduler_organization_discovery_single_thread_no_account_crawl(self):
        calls = []

        class Tracked(DemoClient):
            def get(self, path):
                calls.append(path)
                return super().get(path)

        monitor = Monitor(Config(), client_factory=Tracked, selected=())
        monitor.start()
        try:
            wait_for(lambda: monitor.snapshot()["latest"] is not None)
            monitor.discover_organization("example")
            wait_for(lambda: monitor.snapshot()["organization_discovery"]["latest"] is not None)
            self.assertEqual(monitor.snapshot()["selection"]["mode"], "paused")
            self.assertTrue(all(path.startswith("/orgs/example/repos?") for path in calls))
        finally:
            monitor.stop()

    def test_organization_batch_uses_existing_request_budget_and_shared_backoff(self):
        class Paged(DemoClient):
            def get(self, path):
                self.requests += 1
                return [{"full_name": f"org/r{i}"} for i in range(100)]

        monitor = Monitor(Config(discovery_requests=1), client_factory=Paged, selected=())
        monitor.discover_organization("org")
        monitor._discover_organization()
        discovery = monitor.snapshot()["organization_discovery"]
        self.assertFalse(discovery["latest"]["complete"])
        self.assertTrue(discovery["pending"])
        self.assertEqual(discovery["latest"]["next_page"], 2)
        self.assertAlmostEqual(monitor._organization_due - time.monotonic(), 5, delta=0.5)
        with patch("impact.monitor.inventory", return_value={"reason": {"kind": "rate_limit"}}):
            monitor._backoff.until = time.monotonic() + 1000
            monitor._discover_organization()
            self.assertGreater(monitor._organization_due - time.monotonic(), 990)

    def test_unexpected_discovery_error_is_visible_and_sanitized(self):
        monitor = Monitor(Config(), client_factory=DemoClient, selected=())
        monitor.discover_organization("example")
        with patch("impact.monitor.inventory", side_effect=RuntimeError("secret")), patch("impact.monitor.sys.stderr"):
            monitor._discover_organization()
        discovery = monitor.snapshot()["organization_discovery"]
        self.assertEqual(discovery["error"]["kind"], "RuntimeError")
        self.assertNotIn("secret", str(discovery))

    def test_credential_change_discards_old_catalog_and_queues_rediscovery(self):
        monitor = Monitor(Config(), selected=())
        with patch("impact.monitor.credentials", return_value=("old-secret", "test")), patch("impact.monitor.Client", return_value=DemoClient(monitor.config, monitor.stop_event)):
            monitor.discover_organization("example")
            monitor._discover_organization()
        self.assertEqual(monitor.snapshot()["organization_discovery"]["latest"]["discovered"], 3)
        with patch("impact.monitor.credentials", return_value=("new-secret", "test")), patch("impact.monitor.Client", return_value=DemoClient(monitor.config, monitor.stop_event)):
            monitor._new_client(monitor.config)
        discovery = monitor.snapshot()["organization_discovery"]
        self.assertIsNone(discovery["latest"])
        self.assertTrue(discovery["pending"])
        self.assertNotIn("secret", str(monitor.snapshot()))
