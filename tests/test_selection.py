import threading
import unittest
from unittest.mock import patch

from impact.app import main
from impact.client import Config, InventoryState, ProbeError, inventory, repository_selection
from impact.demo import DemoClient
from impact.monitor import Monitor
from tests.test_assessment import FixtureClient
from tests.test_monitor_http import wait_for


class SelectionTests(unittest.TestCase):
    def test_exact_names_only_and_case_insensitive_deduplication(self):
        self.assertEqual(repository_selection(["Org/Repo", "org/repo", "org/other"], 200),
                         ("Org/Repo", "org/other"))
        for value in [
            [], None, "org/repo", [True], ["org"], ["org/repo/other"], ["https://github.com/org/repo"],
            ["org/repo?x=1"], ["org/repo#frag"], ["org/.."], ["../repo"], ["org/."],
            ["org/%2e%2e"], ["org\\repo"], ["org/ repo"], ["org/" + "a" * 101],
            ["org/repo\n"], ["org/repo"] * 201,
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                repository_selection(value, 200)
        with self.assertRaises(ValueError):
            repository_selection(["org/one", "org/two"], 1)

    def test_selected_inventory_never_enumerates_account_or_org(self):
        client = FixtureClient({
            "/repos/org/one": {"full_name": "org/one", "has_issues": True, "has_pages": False},
            "/repos/org/two": {"full_name": "org/two", "has_issues": False, "has_pages": True},
        })
        report = inventory(client, selected=("org/one", "org/two"))
        self.assertTrue(report["complete"])
        self.assertEqual(client.calls, ["/repos/org/one", "/repos/org/two"])
        self.assertEqual(report["discovered"], 2)
        self.assertIn("No account or organization", report["scope"])
        self.assertFalse(report["repositories"][0]["has_pages"])

    def test_selected_validation_resumes_within_request_budget(self):
        state = InventoryState()
        names = ("org/one", "org/two", "org/three")
        responses = {f"/repos/{name}": {"full_name": name} for name in names}
        first = FixtureClient(responses)
        result = inventory(first, selected=names, state=state, request_limit=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["next_repository"], 2)
        second = FixtureClient(responses)
        result = inventory(second, selected=names, state=state, request_limit=2)
        self.assertTrue(result["complete"])
        self.assertEqual(second.calls, ["/repos/org/two", "/repos/org/three"])
        self.assertEqual(result["discovered"], 3)
        self.assertIsNone(result["next_repository"])

    def test_deadline_and_rate_limits_remain_checkpoints(self):
        client = FixtureClient({"/repos/org/one": ProbeError("rate_limit", "Wait for rate limit")})
        state = InventoryState()
        report = inventory(client, selected=("org/one",), state=state, deadline=0)
        self.assertEqual(report["reason"]["kind"], "inventory_deadline")
        self.assertEqual(client.calls, [])
        report = inventory(client, selected=("org/one",), state=state)
        self.assertEqual(report["reason"]["kind"], "rate_limit")
        self.assertEqual(report["next_repository"], 1)
        self.assertFalse(report["complete"])

    def test_failed_or_renamed_selected_repositories_are_visible_gaps(self):
        for invalid in [
            {}, [], {"full_name": "other/repo"},
            ProbeError("permission_or_secondary_limit", "Permission denied"),
            ProbeError("not_found_or_hidden", "Missing or hidden"),
        ]:
            with self.subTest(invalid=invalid):
                client = FixtureClient({
                    "/repos/org/one": invalid, "/repos/org/two": {"full_name": "org/two"},
                })
                result = inventory(client, selected=("org/one", "org/two"))
                self.assertFalse(result["complete"])
                self.assertEqual(result["reason"]["kind"], "selected_repository_errors")
                self.assertEqual(result["repository_errors"][0]["repository"], "org/one")
                self.assertEqual([repo["name"] for repo in result["repositories"]], ["org/two"])

    def test_selected_failures_do_not_retain_stale_repositories(self):
        state = InventoryState()
        names = ("org/one",)
        inventory(FixtureClient({"/repos/org/one": {"full_name": "org/one"}}), selected=names, state=state)
        result = inventory(FixtureClient({"/repos/org/one": ProbeError("authentication", "No access")}),
                           selected=names, state=state)
        self.assertEqual(result["repositories"], [])
        self.assertFalse(result["complete"])

    def test_paused_monitor_does_not_make_repository_requests(self):
        calls = []

        class NoRepoDemo(DemoClient):
            def get(self, path):
                calls.append(path)
                raise AssertionError("Paused scope must not read repositories")

        monitor = Monitor(Config(), client_factory=NoRepoDemo, selected=())
        monitor._cycle()
        self.assertEqual(calls, [])
        state = monitor.snapshot()
        self.assertEqual(state["selection"]["mode"], "paused")
        self.assertEqual(state["latest"]["checks"], [])
        self.assertTrue(state["latest"]["status_available"])

    def test_monitor_only_checks_selected_repositories_and_queues_changes(self):
        calls = []

        class TrackedDemo(DemoClient):
            def get(self, path):
                calls.append(path)
                return super().get(path)

        monitor = Monitor(Config(), client_factory=TrackedDemo, selected=("example/queue-symptom",))
        monitor._cycle()
        first = monitor.snapshot()
        self.assertEqual(first["selection"]["mode"], "selected")
        self.assertEqual({row["repository"] for row in first["latest"]["checks"]}, {"example/queue-symptom"})
        self.assertTrue(all(path.startswith("/repos/example/queue-symptom") for path in calls))
        monitor.select_repositories("selected", ["example/self-hosted"])
        self.assertIsNotNone(monitor.snapshot()["selection"]["pending"])
        calls.clear()
        monitor._cycle()
        second = monitor.snapshot()
        self.assertEqual({row["repository"] for row in second["latest"]["checks"]}, {"example/self-hosted"})
        self.assertTrue(all(path.startswith("/repos/example/self-hosted") for path in calls))
        self.assertEqual(len(second["history"]), 1)
        self.assertIsNone(second["selection"]["pending"])
        calls.clear()
        monitor.select_repositories("paused", [])
        monitor._cycle()
        self.assertEqual(calls, [])
        self.assertEqual(monitor.snapshot()["latest"]["checks"], [])

    def test_scope_switch_waits_for_inflight_batch_and_discards_old_inventory(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class BlockingDemo(DemoClient):
            def get(self, path):
                calls.append(path)
                if path.startswith("/user/repos?"):
                    entered.set()
                    release.wait(3)
                return super().get(path)

        monitor = Monitor(Config(), client_factory=BlockingDemo)
        monitor.start()
        try:
            self.assertTrue(entered.wait(2))
            monitor.select_repositories("selected", ["example/self-hosted"])
            self.assertEqual(monitor.snapshot()["selection"]["pending"]["mode"], "selected")
            release.set()
            wait_for(lambda: monitor.snapshot()["selection"]["mode"] == "selected"
                     and monitor.snapshot()["latest"] is not None)
            snapshot = monitor.snapshot()
            self.assertEqual([repo["name"] for repo in snapshot["latest"]["inventory"]["repositories"]],
                             ["example/self-hosted"])
            self.assertEqual(sum(path.startswith("/user/repos?") for path in calls), 1)
        finally:
            release.set()
            monitor.stop()

    def test_invalid_scope_changes_keep_original_scope(self):
        monitor = Monitor(Config(), selected=())
        for mode, names in [
            ("invalid", []), ("all", ["org/repo"]), ("paused", ["org/repo"]),
            ("selected", []), ("selected", ["../repo"]),
        ]:
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                monitor.select_repositories(mode, names)
            self.assertIsNone(monitor.snapshot()["selection"]["pending"])
        monitor.stop()
        self.assertEqual(monitor.select_repositories("selected", ["org/repo"]), "stopped")

    def test_identical_scope_submissions_do_not_restart_work(self):
        monitor = Monitor(Config(), client_factory=DemoClient, selected=("example/queue-symptom",))
        monitor._cycle()
        self.assertEqual(monitor.select_repositories("selected", ["example/queue-symptom"]), "unchanged")
        self.assertIsNone(monitor.snapshot()["selection"]["pending"])
        monitor.select_repositories("selected", ["example/self-hosted"])
        self.assertEqual(monitor.select_repositories("selected", ["example/self-hosted"]), "unchanged")

    def test_all_scope_is_explicit_and_restores_existing_discovery(self):
        monitor = Monitor(Config(), client_factory=DemoClient, selected=())
        self.assertFalse(monitor.snapshot()["selection"]["discovery_opted_in"])
        monitor.select_repositories("selected", ["example/queue-symptom"])
        monitor._cycle()
        self.assertFalse(monitor.snapshot()["selection"]["discovery_opted_in"])
        monitor.select_repositories("all", [])
        self.assertTrue(monitor.snapshot()["selection"]["discovery_opted_in"])
        monitor._cycle()
        self.assertEqual(monitor.snapshot()["selection"]["mode"], "all")
        self.assertEqual(monitor.snapshot()["latest"]["inventory"]["discovered"], 3)
        monitor.select_repositories("paused", [])
        monitor._cycle()
        self.assertTrue(monitor.snapshot()["selection"]["discovery_opted_in"])
        fresh = Monitor(Config(), client_factory=DemoClient, selected=())
        self.assertFalse(fresh.snapshot()["selection"]["discovery_opted_in"])

    def test_cli_defaults_to_paused_and_accepts_explicit_startup_scopes(self):
        for args, selected in [
            ([], ()), (["--repos", "org/one", "org/two"], ("org/one", "org/two")),
            (["--all-repositories"], None), (["--demo"], None),
        ]:
            with self.subTest(args=args), patch("sys.argv", ["impact", *args]), patch(
                "impact.app.Monitor",
            ) as monitor, patch("impact.app.SelectionStore") as store, patch("impact.app.ObservationStore"), patch(
                "impact.app.make_server", side_effect=OSError,
            ), patch("impact.app.sys.stderr"):
                store.return_value.load.return_value = ()
                self.assertEqual(main(), 1)
                self.assertEqual(monitor.call_args.kwargs["selected"], selected)
        for args in [["--repos", "../repo"], ["--repos", "org/repo", "--all-repositories"]]:
            with patch("sys.argv", ["impact", *args]), patch("impact.app.sys.stderr"), self.assertRaises(SystemExit):
                main()


if __name__ == "__main__":
    unittest.main()
