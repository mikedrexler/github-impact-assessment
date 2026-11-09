import copy
import threading
import unittest
from datetime import datetime, timezone

from impact.assessment import adapter_for, assess, parsed, probe, relevant
from impact.client import Config, ProbeError
from impact.demo import DemoClient

NOW = datetime(2026, 11, 9, 18, 50, tzinfo=timezone.utc)
INCIDENT = {"id": "incident", "name": "Actions incident", "started_at": "2026-11-09T18:10:00Z"}
REPO = {"name": "example/repo", "url": "https://github.com/example/repo", "has_issues": True, "has_pages": True}
COMPONENT = {"id": "actions", "name": "Actions"}


class FixtureClient:
    def __init__(self, responses=None, status=None, config=None):
        self.config = config or Config()
        self.requests = 0
        self.calls = []
        self.responses = responses or {}
        self.status_data = status or {"components": [], "incidents": []}

    def status(self):
        if isinstance(self.status_data, Exception):
            raise self.status_data
        return copy.deepcopy(self.status_data)

    def get(self, path):
        self.calls.append(path)
        self.requests += 1
        for suffix, value in self.responses.items():
            if suffix in path:
                if isinstance(value, Exception):
                    raise value
                return copy.deepcopy(value)
        raise AssertionError(f"Unconfigured fixture: {path}")


def action_client(run=None, jobs=None, checks=None):
    run = run or {
        "id": 1, "created_at": "2026-11-09T18:15:00Z",
        "updated_at": "2026-11-09T18:49:00Z", "status": "queued", "head_sha": "a" * 40,
    }
    return FixtureClient({
        "/actions/runs?": {"total_count": 1, "workflow_runs": [run]},
        "/jobs?": {"total_count": len(jobs or []), "jobs": jobs or []},
        "/check-runs?": {"total_count": len(checks or []), "check_runs": checks or []},
    })


class AssessmentTests(unittest.TestCase):
    def test_all_current_component_names_and_future_fallback(self):
        expected = {
            "Git Operations": "git", "Webhooks": "webhooks", "API Requests": "api",
            "Issues": "issues", "Pull Requests": "pull_requests", "Actions": "actions",
            "Packages": "packages", "Pages": "pages", "Copilot": "copilot",
            "Codespaces": "codespaces", "Copilot AI Model Providers": "copilot",
            "Visit www.githubstatus.com for more information": "unsupported",
            "Future component": "unsupported",
        }
        for name, adapter in expected.items():
            self.assertEqual(adapter_for(name), adapter)

    def test_incident_mapping_uses_ids_not_titles(self):
        client = FixtureClient({"/user/repos?": [dict(full_name="example/repo")]}, {
            "components": [{"id": "future", "name": "Future component", "status": "major_outage"}, COMPONENT],
            "incidents": [{**INCIDENT, "name": "Actions incident", "components": [{"id": "future"}]}],
        })
        report = assess(client)
        self.assertEqual(len(report["checks"]), 1)
        self.assertEqual(report["checks"][0]["component_id"], "future")
        self.assertEqual(report["checks"][0]["classification"], "unknown")
        self.assertFalse(any("/actions/" in path for path in client.calls))

    def test_no_incident_does_not_fabricate_checks(self):
        client = FixtureClient({"/user/repos?": [dict(full_name="example/repo")]}, {
            "components": [{"id": "a", "name": "Actions", "status": "operational"}], "incidents": [],
        })
        report = assess(client)
        self.assertEqual(report["checks"], [])
        self.assertEqual(report["inventory"]["discovered"], 1)
        self.assertTrue(report["coverage"]["complete"])

    def test_resolved_incidents_are_not_probed(self):
        client = FixtureClient({"/user/repos?": []}, {
            "components": [COMPONENT], "incidents": [{**INCIDENT, "status": "resolved", "components": [COMPONENT]}],
        })
        self.assertEqual(assess(client)["incidents"], [])

    def test_unscoped_and_degraded_without_incident_are_explicit(self):
        client = FixtureClient({"/user/repos?": []}, {
            "components": [{"id": "actions", "name": "Actions", "status": "major_outage"}],
            "incidents": [{**INCIDENT, "components": [{"id": "missing"}]}],
        })
        report = assess(client)
        self.assertEqual({error["kind"] for error in report["errors"]}, {"unscoped_incident", "component_without_incident"})
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["checks"], [])

    def test_status_failure_does_not_mean_no_incident(self):
        client = FixtureClient({"/user/repos?": []}, ProbeError("network", "Status unavailable"))
        report = assess(client)
        self.assertFalse(report["status_available"])
        self.assertEqual(report["errors"][0]["kind"], "network")
        self.assertFalse(report["coverage"]["complete"])

    def test_rotation_and_deferred_repositories(self):
        client = FixtureClient({"/user/repos?": [
            {"full_name": f"example/r{i}"} for i in range(3)
        ]}, {
            "components": [{"id": "copilot", "name": "Copilot", "status": "major_outage"}],
            "incidents": [{**INCIDENT, "components": [{"id": "copilot"}]}],
        }, Config(max_check_repositories=2))
        first = assess(client)
        second = assess(client, first["next_offset"])
        self.assertEqual(first["coverage"]["repositories_deferred"], 1)
        self.assertFalse(first["coverage"]["complete"])
        self.assertEqual(second["checks"][0]["repository"], "example/r2")

    def test_total_assessment_rows_are_bounded(self):
        client = FixtureClient({"/user/repos?": [
            {"full_name": f"example/r{i}"} for i in range(3)
        ]}, {
            "components": [{"id": f"part{i}", "name": f"New area {i}", "status": "major_outage"} for i in range(10)],
            "incidents": [{**INCIDENT, "components": [{"id": f"part{i}"} for i in range(10)]}],
        }, Config(max_assessments=5))
        report = assess(client)
        self.assertEqual(len(report["checks"]), 5)
        self.assertEqual(report["coverage"]["assessments_deferred"], 25)
        self.assertEqual(report["coverage"]["repositories_selected"], 1)
        self.assertFalse(report["coverage"]["complete"])

    def test_timing_handles_historical_active_and_missing_timestamps(self):
        historical = {"created_at": "2026-11-09T17:00:00Z", "updated_at": "2026-11-09T18:00:00Z", "status": "completed"}
        self.assertFalse(relevant(historical, INCIDENT, NOW))
        self.assertTrue(relevant({**historical, "status": "in_progress"}, INCIDENT, NOW))
        self.assertFalse(relevant({**historical, "created_at": "2026-11-10T18:00:00Z"}, INCIDENT, NOW))
        self.assertFalse(relevant(historical, {"id": "missing"}, NOW))
        self.assertIsNone(parsed("2026-11-09"))
        self.assertIsNone(parsed("bad-date"))

    def test_historical_failures_are_not_attributed(self):
        client = action_client({
            "id": 1, "created_at": "2026-11-09T17:00:00Z",
            "updated_at": "2026-11-09T18:00:00Z", "status": "completed", "conclusion": "failure",
        })
        row = probe(client, REPO, COMPONENT, INCIDENT, NOW)
        self.assertEqual(row["classification"], "unknown")
        self.assertIn("historical", row["reason"])
        self.assertEqual(len(client.calls), 1)

    def test_failed_tests_do_not_mean_platform_impact(self):
        client = action_client({
            "id": 1, "created_at": "2026-11-09T18:15:00Z", "updated_at": "2026-11-09T18:40:00Z",
            "status": "completed", "conclusion": "failure", "head_sha": "a" * 40,
        }, checks=[{"conclusion": "failure"}])
        row = probe(client, REPO, COMPONENT, INCIDENT, NOW)
        self.assertEqual(row["classification"], "unknown")
        self.assertTrue(any("not attributed" in item["summary"] for item in row["evidence"]))
        self.assertTrue(any("external apps" in item["summary"] for item in row["evidence"]))

    def test_queue_threshold_self_hosting_and_uncertainty(self):
        for created, labels, expected in [
            ("2026-11-09T18:40:00Z", ["ubuntu-latest"], "suspected"),
            ("2026-11-09T18:40:01Z", ["ubuntu-latest"], "unknown"),
            ("2026-11-09T18:20:00Z", ["self-hosted", "linux"], "unknown"),
            ("2026-11-09T17:50:00Z", ["ubuntu-latest"], "unknown"),
            (None, ["ubuntu-latest"], "unknown"),
        ]:
            with self.subTest(created=created, labels=labels):
                client = action_client(jobs=[{"id": 2, "status": "queued", "created_at": created, "labels": labels}])
                row = probe(client, REPO, COMPONENT, INCIDENT, NOW)
                self.assertEqual(row["classification"], expected)
                if expected == "suspected":
                    self.assertTrue(any("not continuous queue duration" in item["summary"] for item in row["evidence"]))

    def test_successful_reads_never_emit_unaffected(self):
        for name, suffix, response in [
            ("API Requests", "/repos/example/repo", {}),
            ("Git Operations", "/commits?", []),
            ("Pull Requests", "/pulls?", []),
            ("Issues", "/issues?", []),
            ("Pages", "/pages", {}),
        ]:
            with self.subTest(name=name):
                row = probe(FixtureClient({suffix: response}), REPO, {"id": name, "name": name}, INCIDENT, NOW)
                self.assertEqual(row["classification"], "unknown")
                self.assertIn("cannot prove", row["reason"])

    def test_unsupported_probes_never_request_more_permissions(self):
        client = FixtureClient()
        for name in ["Webhooks", "Packages", "Copilot", "Codespaces", "Copilot AI Model Providers", "New area"]:
            row = probe(client, REPO, {"id": name, "name": name}, INCIDENT, NOW)
            self.assertEqual(row["classification"], "unknown")
        self.assertEqual(client.calls, [])

    def test_disabled_features_not_applicable(self):
        for name in ["Issues", "Pages"]:
            row = probe(FixtureClient(), {**REPO, "has_issues": False, "has_pages": False},
                        {"id": name, "name": name}, INCIDENT, NOW)
            self.assertEqual(row["classification"], "not-applicable")

    def test_unknown_feature_flags_are_probed_not_disabled(self):
        client = FixtureClient({"/pages": {}, "/issues?": []})
        for name in ["Issues", "Pages"]:
            row = probe(client, {**REPO, "has_issues": None, "has_pages": None},
                        {"id": name, "name": name}, INCIDENT, NOW)
            self.assertEqual(row["classification"], "unknown")
        self.assertEqual(len(client.calls), 2)

    def test_probe_errors_remain_unknown(self):
        for kind in ["authentication", "permission_or_secondary_limit", "network", "rate_limit", "deadline", "service_error"]:
            client = FixtureClient({"/actions/runs?": ProbeError(kind, "Cannot read")})
            row = probe(client, REPO, COMPONENT, INCIDENT, NOW)
            self.assertEqual(row["classification"], "unknown")
            self.assertEqual(row["error_kind"], kind)
            self.assertIsNotNone(row["checked_at"])

    def test_demo_covers_queue_self_hosted_and_permissions(self):
        client = DemoClient(Config(), threading.Event())
        report = assess(client)
        self.assertEqual(len(report["components"]), 12)
        self.assertEqual(report["counts"]["suspected"], 1)
        self.assertEqual(report["counts"]["confirmed"], 0)
        self.assertEqual(report["coverage"]["check_errors"], 1)
        self.assertEqual(len(report["checks"]), 9)

    def test_malformed_workflow_response_is_explicit(self):
        client = FixtureClient({"/actions/runs?": {"workflow_runs": "invalid"}})
        row = probe(client, REPO, COMPONENT, INCIDENT, NOW)
        self.assertEqual(row["error_kind"], "invalid_response")

    def test_malformed_status_field_is_per_probe_error(self):
        client = action_client({"id": 1, "status": {"unexpected": True}})
        row = probe(client, REPO, COMPONENT, INCIDENT, NOW)
        self.assertEqual(row["error_kind"], "invalid_response")
        self.assertEqual(row["classification"], "unknown")


if __name__ == "__main__":
    unittest.main()
