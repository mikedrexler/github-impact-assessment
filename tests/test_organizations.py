import threading
import time
import unittest
from unittest.mock import patch

from impact.client import Config, ProbeError
from impact.demo import DemoClient
from impact.monitor import Monitor
from impact.organizations import OrganizationState, organizations
from tests.test_assessment import FixtureClient


class OrganizationTests(unittest.TestCase):
    def load(self, client, state=None, limit=10):
        return organizations(client, state or OrganizationState(),
                             deadline=time.monotonic() + 10, request_limit=limit)

    def test_memberships_and_outside_collaborator_owners_without_personal_accounts(self):
        client = FixtureClient({
            "/user/orgs?": [{"login": "MemberOrg"}, {"login": "empty-org"}],
            "/user/repos?": [
                {"owner": {"login": "memberorg", "type": "Organization"}},
                {"owner": {"login": "external-org", "type": "Organization"}},
                {"owner": {"login": "personal", "type": "User"}},
            ],
        })
        result = self.load(client)
        self.assertEqual(result["organizations"], ["empty-org", "external-org", "memberorg"])
        self.assertEqual(result["owners"], ["empty-org", "external-org", "memberorg", "personal"])
        self.assertTrue(result["complete"])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(path.startswith(("/user/orgs?", "/user/repos?")) for path in client.calls))

    def test_pagination_checkpoints_resume_both_sources_without_repeating_pages(self):
        state = OrganizationState()
        first = FixtureClient({"/user/orgs?": [{"login": f"org-{i}"} for i in range(100)]})
        self.assertFalse(self.load(first, state, 1)["finished"])
        second = FixtureClient({"/user/orgs?": [], "/user/repos?": []})
        self.assertTrue(self.load(second, state)["complete"])
        self.assertIn("page=2", second.calls[0])
        self.assertEqual(len(state.organizations), 100)

    def test_membership_endpoint_denied_still_uses_repository_owners_with_explicit_gap(self):
        result = self.load(FixtureClient({
            "/user/orgs?": ProbeError("permission_or_secondary_limit", "Membership list unavailable"),
            "/user/repos?": [{"owner": {"login": "outside", "type": "Organization"}}],
        }))
        self.assertEqual(result["organizations"], ["outside"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["finished"])
        self.assertEqual(result["errors"][0]["source"], "memberships")

    def test_rate_limit_retains_cursor_and_results_for_resume(self):
        state = OrganizationState()
        state.organizations["known"] = "known"
        result = self.load(FixtureClient({"/user/orgs?": ProbeError("rate_limit", "Budget exhausted")}), state)
        self.assertFalse(result["finished"])
        self.assertEqual(result["reason"]["kind"], "rate_limit")
        self.assertEqual(result["organizations"], ["known"])
        self.assertEqual((state.source, state.page), (0, 1))

    def test_invalid_login_capacity_and_sso_are_partial_not_success(self):
        for rows, limit in [([{"login": "bad/path"}], 10), ([{"login": "one"}, {"login": "two"}], 1)]:
            result = self.load(FixtureClient({"/user/orgs?": rows}, config=Config(max_repositories=limit)))
            self.assertFalse(result["complete"])
            self.assertIsNotNone(result["reason"])
        client = FixtureClient({"/user/orgs?": [], "/user/repos?": []})
        client.access_warnings = {"SSO"}
        self.assertFalse(self.load(client)["complete"])
        missing_owner = self.load(FixtureClient({"/user/orgs?": [], "/user/repos?": [{}]}))
        self.assertEqual(missing_owner["reason"]["kind"], "invalid_response")
        self.assertFalse(missing_owner["complete"])

    def test_page_cap_stops_incomplete_and_hourly_refresh_replaces_removed_organizations(self):
        state = OrganizationState(source=1, page=1000)
        client = FixtureClient({"/user/repos?": [
            {"owner": {"login": "personal", "type": "User"}} for _ in range(100)
        ]})
        report = self.load(client, state)
        self.assertTrue(report["finished"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["reason"]["kind"], "organization_limit")
        monitor = Monitor(Config(), client_factory=DemoClient, selected=())
        monitor._organizations.organizations["removed"] = "removed"
        monitor._organizations_report = {"finished": True}
        monitor._load_organizations()
        self.assertNotIn("removed", monitor.snapshot()["organizations"]["latest"]["organizations"])
        self.assertGreater(monitor._organizations_due - time.monotonic(), 3590)

    def test_scheduler_automatically_populates_while_paused_without_applying_repositories(self):
        monitor = Monitor(Config(), client_factory=DemoClient, demo=True, selected=())
        monitor.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and monitor.snapshot()["organizations"]["latest"] is None:
                threading.Event().wait(0.01)
            snapshot = monitor.snapshot()
            self.assertEqual(snapshot["organizations"]["latest"]["organizations"], ["example", "fixture-membership"])
            self.assertEqual(snapshot["selection"]["mode"], "paused")
            self.assertEqual(snapshot["selection"]["repositories"], [])
            self.assertIsNone(snapshot["organization_discovery"]["latest"])
        finally:
            monitor.stop()

    def test_credential_rotation_clears_previous_organization_options(self):
        monitor = Monitor(Config(), selected=())
        monitor._organizations.organizations["old-org"] = "old-org"
        monitor._organizations_report = {"organizations": ["old-org"], "finished": True}
        with patch("impact.monitor.credentials", return_value=("new-test-token", "test")):
            monitor._new_client(Config())
        self.assertIsNone(monitor.snapshot()["organizations"]["latest"])
        self.assertEqual(monitor._organizations.organizations, {})
