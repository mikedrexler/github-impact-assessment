import unittest

from impact.client import Config, InventoryState, has_write_access, inventory
from impact.demo import DemoClient
from impact.monitor import Monitor
from tests.test_assessment import FixtureClient


class WritableTests(unittest.TestCase):
    def test_selected_organization_loads_directly_without_account_traversal(self):
        calls = []

        class Scoped(DemoClient):
            def get(self, path):
                calls.append(path)
                if path.startswith("/orgs/octodemo/repos?"):
                    return [{"full_name": "octodemo/example", "permissions": {"push": True}},
                            {"full_name": "octodemo/readonly", "permissions": {"pull": True}}]
                raise AssertionError(f"Unexpected account-wide request: {path}")

        monitor = Monitor(Config(), client_factory=Scoped, selected=())
        before = monitor.snapshot()["selection"]
        self.assertEqual(monitor.discover_writable_repositories("Octodemo"), "accepted")
        monitor._discover_organization()
        report = monitor.snapshot()["organization_discovery"]
        self.assertEqual(report["organization"], "octodemo")
        self.assertEqual([repo["name"] for repo in report["latest"]["repositories"]], ["octodemo/example"])
        self.assertTrue(report["latest"]["complete"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(monitor.snapshot()["selection"], before)

    def test_strict_permission_checks(self):
        for role in ["push", "maintain", "admin"]:
            self.assertTrue(has_write_access({"permissions": {role: True}}))
        for permissions in [None, [], {}, {"pull": True}, {"triage": True}, {"push": 1},
                            {"admin": "true"}, {"push": False}]:
            self.assertFalse(has_write_access({"permissions": permissions}))

    def test_personal_organization_collaborator_catalog_filters_unknown_and_read_only(self):
        rows = [
            {"full_name": "me/personal", "permissions": {"admin": True}},
            {"full_name": "org/project", "permissions": {"push": True}},
            {"full_name": "friend/shared", "permissions": {"maintain": True}},
            {"full_name": "org/readonly", "permissions": {"pull": True}},
            {"full_name": "org/unknown"},
        ]
        client = FixtureClient({"/user/repos?": rows})
        report = inventory(client, writable=True)
        self.assertTrue(report["complete"])
        self.assertEqual({row["name"] for row in report["repositories"]},
                         {"me/personal", "org/project", "friend/shared"})
        self.assertTrue(all(row["write_access"] for row in report["repositories"]))
        self.assertEqual(len(client.calls), 1)
        self.assertIn("affiliation=owner,collaborator,organization_member", client.calls[0])

    def test_readonly_full_page_still_continues_to_writable_next_page(self):
        state = InventoryState()
        first = FixtureClient({"/user/repos?": [
            {"full_name": f"org/r{i}", "permissions": {"pull": True}} for i in range(100)
        ]})
        report = inventory(first, writable=True, state=state, request_limit=1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["next_page"], 2)
        self.assertEqual(report["repositories"], [])
        second = FixtureClient({"/user/repos?": [{"full_name": "personal/repo", "permissions": {"push": True}}]})
        report = inventory(second, writable=True, state=state)
        self.assertTrue(report["complete"])
        self.assertEqual(report["repositories"][0]["name"], "personal/repo")

    def test_manual_selected_validation_denies_readonly_unknown_and_revoked_access(self):
        names = ("org/repo",)
        state = InventoryState()
        for permission, count in [({"push": True}, 1), ({"pull": True}, 0), (None, 0), ({"admin": True}, 1)]:
            client = FixtureClient({"/repos/org/repo": {"full_name": "org/repo", "permissions": permission}})
            report = inventory(client, selected=names, writable=True, state=state)
            self.assertEqual(len(report["repositories"]), count)
            if not count:
                self.assertEqual(report["repository_errors"][0]["kind"], "write_access_required")
            self.assertEqual(client.calls, ["/repos/org/repo"])

    def test_partial_catalog_refresh_removes_explicitly_revoked_cached_permission(self):
        state = InventoryState()
        inventory(FixtureClient({"/user/repos?": [{"full_name": "org/repo", "permissions": {"push": True}}]}),
                  writable=True, state=state)
        rows = [{"full_name": "ORG/repo", "permissions": {"pull": True}}]
        rows += [{"full_name": f"org/r{i}", "permissions": {"pull": True}} for i in range(99)]
        result = inventory(FixtureClient({"/user/repos?": rows}), writable=True, state=state, request_limit=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["repositories"], [])

    def test_browser_catalog_never_changes_selection_and_readonly_manual_names_never_assessed(self):
        class Restricted(DemoClient):
            def get(self, path):
                result = super().get(path)
                if path == "/repos/example/queue-symptom":
                    result["permissions"] = {"pull": True}
                return result
        monitor = Monitor(Config(), client_factory=Restricted, selected=("example/queue-symptom",))
        before = monitor.snapshot()["selection"]
        self.assertEqual(monitor.discover_writable_repositories(), "accepted")
        self.assertEqual(monitor.discover_writable_repositories(), "already_running")
        monitor._discover_organization()
        self.assertEqual(monitor.snapshot()["selection"], before)
        self.assertTrue(monitor.snapshot()["organization_discovery"]["writable"])
        monitor._cycle()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["latest"]["checks"], [])
        self.assertEqual(snapshot["latest"]["observations"]["rows"][0]["gap"], "metadata_unavailable")
        self.assertEqual(snapshot["discovery"]["latest"]["repository_errors"][0]["kind"], "write_access_required")
