import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from impact.client import Client, Backoff, Config, ProbeError
from impact.demo import DemoClient
from impact.monitor import Monitor
from impact.observations import ObservationStore, Observations, metrics, FRESH_SECONDS
from tests.test_client import Opener, Response, NoWait, http_error


class BudgetTests(unittest.TestCase):
    def test_minute_budget_shared_by_discovery_assessment_and_planner(self):
        guard = Backoff()
        opener = Opener(Response({}), Response({}))
        config = Config(github_requests_minute=2)
        first = Client("test", config, NoWait(), opener=opener, backoff=guard, clock=lambda: 10)
        second = Client("test", config, NoWait(), opener=opener, backoff=guard, clock=lambda: 10)
        first.get("/repos/org/a")
        second.get_once("/repos/org/b", 100)
        with self.assertRaises(ProbeError) as error:
            first.get("/repos/org/c")
        self.assertEqual(error.exception.kind, "rate_limit")
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(guard.until, 70)
        self.assertEqual(guard.reason, "local_request_budget")

    def test_hourly_budget_does_not_reset_with_client_or_minute(self):
        guard = Backoff()
        config = Config(github_requests_hour=1)
        opener = Opener(Response({}), Response({}))
        Client("test", config, NoWait(), opener=opener, backoff=guard, clock=lambda: 10).get("/user")
        with self.assertRaises(ProbeError):
            Client("test", config, NoWait(), opener=opener, backoff=guard, clock=lambda: 100).get("/user")
        Client("test", config, NoWait(), opener=opener, backoff=guard, clock=lambda: 3610).get("/user")
        self.assertEqual(len(opener.calls), 2)

    def test_successful_response_reserves_headroom_until_provider_reset(self):
        guard = Backoff()
        opener = Opener(Response({}, {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "1200",
                                      "x-ratelimit-limit": "5000"}))
        client = Client("test", Config(), NoWait(), opener=opener, backoff=guard, clock=lambda: 10)
        with patch("impact.client.time.time", return_value=1000):
            client.get("/user")
        self.assertEqual(guard.until, 212)
        self.assertEqual(guard.reason, "provider_quota_reserve")
        with self.assertRaises(ProbeError):
            client.get("/user")
        self.assertEqual(len(opener.calls), 1)

    def test_out_of_order_headers_never_increase_remaining_and_invalid_headers_are_ignored(self):
        guard = Backoff()
        for remaining in ["100", "120", "90"]:
            guard.observe({"X-RateLimit-Remaining": remaining, "X-RateLimit-Reset": "2000"}, 0)
        self.assertEqual(guard.remaining, 90)
        for reset in ["NaN", "inf", "not a time"]:
            guard.observe({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}, 0)
        self.assertEqual(guard.remaining, 90)
        guard.observe({"X-RateLimit-Remaining": "300", "X-RateLimit-Reset": "3000"}, 0)
        self.assertEqual(guard.remaining, 300)

    def test_retry_attempts_are_charged_and_public_status_does_not_use_github_budget(self):
        guard = Backoff()
        config = Config(github_requests_minute=1)
        opener = Opener(http_error(503), Response({"components": [], "incidents": []}))
        client = Client("test", config, NoWait(), opener=opener, backoff=guard)
        with self.assertRaises(ProbeError) as error:
            client.get("/user")
        self.assertEqual(error.exception.kind, "rate_limit")
        self.assertTrue(client.status()["components"] == [])
        self.assertEqual(len(opener.calls), 2)

    def test_concurrent_clients_cannot_overreserve(self):
        guard = Backoff()
        admitted = []
        def reserve():
            try:
                with guard.lock:
                    guard.reserve(10, Config(github_requests_minute=3))
                admitted.append(True)
            except ProbeError:
                pass
        threads = [threading.Thread(target=reserve) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(admitted), 3)

    def test_http_date_retry_after_is_honored_without_github_retry(self):
        guard = Backoff()
        opener = Opener(http_error(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        client = Client("test", Config(), NoWait(), opener=opener, backoff=guard, clock=lambda: 10)
        with patch("impact.client.time.time", return_value=1445411880):
            with self.assertRaises(ProbeError):
                client.get("/user")
        self.assertEqual(guard.until, 610)
        self.assertEqual(len(opener.calls), 1)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.store = ObservationStore()
        self.tracker = Observations(self.store)
        self.now = 2000000000
        self.names = ("example/queue-symptom",)
        self.repos = [{"name": self.names[0]}]
        self.component = {"id": "actions", "name": "Actions", "status": "operational"}

    def collect(self, *, status=True, incidents=None, cached=None, client=None):
        client = client or DemoClient(Config(), threading.Event())
        with patch("impact.observations.time.time", return_value=self.now):
            return self.tracker.collect(client, "scope", self.names, self.repos, status,
                                        [self.component], incidents or [], cached or {})

    def sample(self, at, **values):
        data = {"outcome": "observed", "baseline_eligible": True, "incident_ids": [],
                "queued_runs": 0, "sampled_runs": 3, "oldest_queued_age_seconds": None,
                "failed_runs": 0, "completed_runs": 3, "sample_partial": True, **values}
        self.store.append("scope", self.names[0], at, data)

    def test_metrics_keep_only_aggregates_and_never_treat_missing_times_as_queue_duration(self):
        data = {"total_count": 100, "workflow_runs": [
            {"status": "queued", "created_at": "bad", "name": "secret", "head_sha": "secret"},
            {"status": "completed", "conclusion": "failure", "html_url": "secret"},
        ]}
        result = metrics(data, self.now)
        self.assertEqual(result["sampled_runs"], 2)
        self.assertEqual(result["failed_runs"], 1)
        self.assertIsNone(result["oldest_queued_age_seconds"])
        self.assertTrue(result["sample_partial"])
        self.assertNotIn("secret", json.dumps(result))
        for bad in [{}, {"workflow_runs": [None]}, {"workflow_runs": [{}] * 31},
                    {"workflow_runs": [{"status": []}]}]:
            with self.subTest(bad=bad), self.assertRaises(ProbeError):
                metrics(bad, self.now)

    def test_continuous_observation_without_incident_does_not_claim_health(self):
        result = self.collect()
        self.assertEqual(result["fresh_observed"], 1)
        self.assertEqual(result["rows"][0]["signal"], "insufficient_baseline")
        self.assertFalse(result["rows"][0]["incident_overlap"])
        self.assertNotIn("classification", result["rows"][0])
        self.assertEqual(len(self.store.records("scope", self.now)), 1)

    def test_baseline_requires_three_preincident_operational_samples(self):
        for at in [self.now - 2000, self.now - 1600, self.now - 1200]:
            self.sample(at)
        self.sample(self.now - 800, baseline_eligible=False, queued_runs=30)
        self.sample(self.now - 400, baseline_eligible=True, queued_runs=30)
        incident = {"id": "incident", "started_at": datetime.fromtimestamp(self.now - 1000, timezone.utc).isoformat(),
                    "components": [{"id": "actions"}]}
        queued_at = datetime.fromtimestamp(self.now - 700, timezone.utc).isoformat()
        path = "/repos/example/queue-symptom/actions/runs?per_page=30&page=1"
        result = self.collect(incidents=[incident], cached={path: {
            "workflow_runs": [{"status": "queued", "created_at": queued_at}] * 4, "total_count": 4,
        }})
        row = result["rows"][0]
        self.assertEqual(row["baseline_samples"], 3)
        self.assertEqual(row["baseline_median_queued_runs"], 0)
        self.assertEqual(row["signal"], "elevated_queue_sample")
        self.assertTrue(row["incident_overlap"])
        self.assertFalse(row["sample"]["baseline_eligible"])

    def test_status_outage_and_degraded_component_samples_do_not_train_baselines(self):
        self.collect(status=False)
        self.now += 400
        self.component["status"] = "degraded_performance"
        self.collect()
        self.assertTrue(all(not row["baseline_eligible"] for row in self.store.records("scope", self.now)))

    def test_budget_failure_stops_batch_and_records_explicit_gap(self):
        client = DemoClient(Config(), threading.Event())
        client.get = lambda path: (_ for _ in ()).throw(ProbeError("rate_limit", "secret"))
        result = self.collect(client=client)
        self.assertEqual(result["fresh_observed"], 0)
        self.assertEqual(result["rows"][0]["gap"], "rate_limit")
        self.assertEqual(result["rows"][0]["signal"], "observation_unavailable")
        self.assertNotIn("secret", str(self.store.records("scope", self.now)))

    def test_sso_partial_results_are_a_gap_and_cannot_train_baseline(self):
        client = DemoClient(Config(), threading.Event())
        client.access_warnings = {"SSO coverage restricted"}
        result = self.collect(client=client)
        row = result["rows"][0]
        self.assertEqual(row["gap"], "sso_partial_results")
        self.assertFalse(row["sample"]["baseline_eligible"])
        self.assertEqual(result["fresh_observed"], 0)

    def test_stale_metadata_missing_empty_and_future_samples_are_not_all_clear(self):
        self.sample(self.now - FRESH_SECONDS - 1)
        self.repos = []
        result = self.collect()
        self.assertEqual(result["gaps"], 1)
        self.assertEqual(result["rows"][0]["gap"], "metadata_unavailable")
        self.repos = [{"name": self.names[0]}]
        self.sample(self.now + 100)
        result = self.collect()
        self.assertFalse(result["rows"][0]["fresh"])
        self.assertEqual(result["rows"][0]["signal"], "observation_unavailable")
        self.names = ("example/unknown",)
        self.repos = [{"name": self.names[0]}]
        result = self.collect(cached={"/repos/example/unknown/actions/runs?per_page=30&page=1": {
            "workflow_runs": [], "total_count": 0,
        }})
        self.assertEqual(result["rows"][0]["signal"], "no_runs_in_sample")

    def test_rotation_oldest_attempts_first_and_missing_metadata_always_visible(self):
        self.names = tuple(f"example/r{i}" for i in range(25)) + ("other/hidden",)
        self.repos = [{"name": name} for name in self.names[:-1]]
        first = self.collect()
        self.assertEqual(first["fresh_observed"], 20)
        self.assertEqual(first["gaps"], 6)
        second = self.collect()
        self.assertEqual(second["fresh_observed"], 25)
        self.assertEqual(second["gaps"], 1)
        self.assertEqual(second["rows"][-1]["gap"], "metadata_unavailable")

    def test_persistence_restart_scope_isolation_retention_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.sqlite3"
            store = ObservationStore(path)
            store.append("scope-a", "org/a", self.now, {"outcome": "observed"})
            restored = ObservationStore(path)
            self.assertEqual(len(restored.records("scope-a", self.now)), 1)
            self.assertEqual(restored.records("scope-b", self.now), [])
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("impact.observations.MAX_RECORDS", 2):
                for at in range(3):
                    restored.append("scope-a", "org/a", self.now + at, {"outcome": "observed"})
            self.assertEqual(len(restored.records("scope-a", self.now)), 2)
            restored.append("scope-a", "org/a", self.now + 15 * 86400, {"outcome": "observed"})
            self.assertEqual(len(restored.records("scope-a", self.now + 15 * 86400)), 1)

    def test_recording_failure_does_not_silently_report_success_or_stop_incident_checks(self):
        store = ObservationStore()
        with patch.object(store, "append", side_effect=sqlite3.OperationalError("secret")):
            monitor = Monitor(Config(), client_factory=DemoClient, selected=self.names, observation_store=store)
            with patch("sys.stderr"):
                monitor._cycle()
        report = monitor.snapshot()["latest"]
        self.assertIsNotNone(report)
        self.assertFalse(report["observations"]["enabled"])
        self.assertIn("failed", report["observations"]["error"])
        self.assertNotIn("secret", json.dumps(report))

    def test_paused_and_all_scope_do_not_make_continuous_observations(self):
        for names in [(), None]:
            store = ObservationStore()
            monitor = Monitor(Config(), client_factory=DemoClient, selected=names, observation_store=store)
            monitor._cycle()
            self.assertFalse(monitor.snapshot()["latest"]["observations"]["enabled"])
            self.assertEqual(store._rows, [])

    def test_incident_reads_reused_without_extra_actions_run_list_request(self):
        calls = []
        class Tracked(DemoClient):
            def get(self, path):
                calls.append(path)
                return super().get(path)
        monitor = Monitor(Config(), client_factory=Tracked, selected=self.names)
        monitor._cycle()
        self.assertEqual(sum("/actions/runs?" in path for path in calls), 1)
        self.assertEqual(monitor.snapshot()["latest"]["observations"]["fresh_observed"], 1)

    def test_no_incident_collection_and_metadata_revalidation_cadence(self):
        class Healthy(DemoClient):
            def status(self):
                data = super().status()
                data["incidents"] = []
                for component in data["components"]:
                    component["status"] = "operational"
                return data
            def past_incidents(self):
                return {"incidents": []}
        monitor = Monitor(Config(), client_factory=Healthy, selected=self.names)
        monitor._cycle()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["latest"]["checks"], [])
        self.assertEqual(snapshot["latest"]["observations"]["fresh_observed"], 1)
        self.assertGreater(monitor._discovery_due - time.monotonic(), 3500)

    def test_exhaustion_advances_incident_rotation_instead_of_repeating_first_repo(self):
        from impact.assessment import assess

        class Limited(DemoClient):
            def get(self, path):
                if "/repos/example/a" in path:
                    return super().get(path.replace("example/a", "example/queue-symptom"))
                raise ProbeError("rate_limit", "Deferred")

        client = Limited(Config(), threading.Event())
        report = assess(client, inventory_report={
            "repositories": [{"name": f"example/{name}", "url": f"https://github.com/example/{name}",
                              "has_issues": True, "has_pages": True} for name in ("a", "b", "c")],
            "complete": True,
        })
        self.assertEqual(report["next_offset"], 1)
        self.assertEqual(report["coverage"]["rate_or_budget_deferred_repositories"], 2)
        self.assertFalse(report["coverage"]["complete"])

    def test_history_restored_only_for_same_credential_and_selected_set(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.sqlite3"
            for credential, expected in [("same", 1), ("same", 1), ("changed", 2)]:
                monitor = Monitor(Config(), selected=self.names, observation_store=ObservationStore(path))
                with patch("impact.monitor.credentials", return_value=(credential, "test")), patch(
                    "impact.monitor.Client", side_effect=lambda token, config, stop, **kwargs: DemoClient(config, stop)
                ):
                    monitor._cycle()
                self.assertEqual(monitor.snapshot()["latest"]["observations"]["fresh_observed"], 1)
                with closing(sqlite3.connect(path)) as database:
                    self.assertEqual(database.execute("SELECT COUNT(DISTINCT scope) FROM observations").fetchone()[0], expected)
