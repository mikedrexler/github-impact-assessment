"""Real public incident timeline replay; all repository/job evidence is synthetic."""

import json
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from impact.assessment import assess, parsed, probe
from tests.test_assessment import FixtureClient, REPO, action_client

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "incident_2026_11_09.json").read_text())
INCIDENT = FIXTURE["incident"]
ACTIONS = next(part for part in INCIDENT["components"] if part["name"] == "Actions")


def snapshot(update):
    parts = [
        {**part, "status": update["components"].get(part["id"], "operational")}
        for part in INCIDENT["components"]
    ]
    incident = {
        **INCIDENT, "status": update["status"], "updated_at": update["at"],
        "components": [part for part in parts if part["id"] in update["components"]],
    }
    if update["status"] != "resolved":
        incident.pop("resolved_at")
    return {"components": parts, "incidents": [incident]}


class November9Tests(unittest.TestCase):
    def test_published_incident_phase_mapping_and_resolution(self):
        for update in FIXTURE["updates"]:
            with self.subTest(at=update["at"], status=update["status"]):
                client = FixtureClient({
                    "/user/repos?": [{"full_name": "example/replay", "has_pages": True, "has_issues": True}],
                    "/actions/runs?": {"total_count": 0, "workflow_runs": []},
                    "/pages": {},
                    "/hooks?": [],
                }, snapshot(update))
                report = assess(client)
                assessed_ids = {part["id"] for part in snapshot(update)["components"] if part["name"] in {"Actions", "Webhooks"}}
                expected = set(update["components"]) & assessed_ids if update["status"] != "resolved" else set()
                self.assertEqual({row["component_id"] for row in report["checks"]}, expected)
                self.assertEqual(report["errors"], [])
                self.assertEqual(report["counts"]["confirmed"], 0)
                self.assertEqual(report["counts"]["suspected"], 0)
                if update["status"] == "resolved":
                    self.assertEqual(report["incidents"], [])
                    self.assertFalse(any("/actions/" in path for path in client.calls))
                else:
                    self.assertTrue(all(row["classification"] == "unknown" for row in report["checks"]))

    def test_time_frozen_dashboard_assessment_detects_only_synthetic_queue_correlation(self):
        at = parsed("2026-11-09T18:30:17.563Z")

        class FrozenTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return at

        client = action_client(jobs=[{
            "id": 201, "status": "queued", "created_at": "2026-11-09T18:15:00Z",
            "labels": ["ubuntu-latest"],
        }])
        client.responses["/user/repos?"] = [{"full_name": REPO["name"], "has_pages": True, "has_issues": True}]
        client.responses["/pages"] = {}
        client.responses["/hooks?"] = []
        client.status_data = snapshot(FIXTURE["updates"][3])
        with patch("impact.assessment.datetime", FrozenTime):
            report = assess(client)
        self.assertEqual(report["counts"], {"confirmed": 0, "suspected": 1, "unknown": 1, "not-applicable": 0})
        action = next(row for row in report["checks"] if row["component"] == "Actions")
        self.assertIn("correlation only", action["reason"])
        self.assertEqual(action["incident_id"], INCIDENT["id"])
        self.assertEqual(report["completed_at"], at.isoformat())

    def test_queue_classification_with_real_incident_start_and_synthetic_jobs(self):
        now = parsed("2026-11-09T18:30:17.563Z")
        for created, labels, expected in [
            ("2026-11-09T18:15:00Z", ["ubuntu-latest"], "suspected"),
            ("2026-11-09T18:15:00Z", ["self-hosted"], "unknown"),
            ("2026-11-09T18:25:00Z", ["ubuntu-latest"], "unknown"),
            ("2026-11-09T18:10:00Z", ["ubuntu-latest"], "unknown"),
            ("2026-11-09T18:35:00Z", ["ubuntu-latest"], "unknown"),
            (None, ["ubuntu-latest"], "unknown"),
        ]:
            with self.subTest(created=created, labels=labels):
                client = action_client(jobs=[{
                    "id": 201, "status": "queued", "created_at": created, "labels": labels,
                }])
                row = probe(client, REPO, ACTIONS, INCIDENT, now)
                self.assertEqual(row["classification"], expected)

    def test_historical_and_incident_time_application_failures_not_attributed(self):
        now = parsed("2026-11-09T18:30:17.563Z")
        for updated in ["2026-11-09T18:10:00Z", "2026-11-09T18:25:00Z"]:
            client = action_client({
                "id": 101, "status": "completed", "conclusion": "failure",
                "created_at": "2026-11-09T18:00:00Z", "updated_at": updated,
                "head_sha": "a" * 40,
            }, checks=[{"conclusion": "failure"}])
            row = probe(client, REPO, ACTIONS, INCIDENT, now)
            self.assertEqual(row["classification"], "unknown")
            if updated < INCIDENT["started_at"]:
                self.assertEqual(len(client.calls), 1)
            else:
                self.assertTrue(any("not attributed" in item["summary"] for item in row["evidence"]))

    def test_incident_title_does_not_invent_api_requests_component(self):
        data = snapshot(FIXTURE["updates"][3])
        data["components"].append({"id": "api", "name": "API Requests", "status": "operational"})
        client = FixtureClient({
            "/user/repos?": [{"full_name": "example/replay"}],
            "/actions/runs?": {"total_count": 0, "workflow_runs": []},
            "/pages": {},
            "/hooks?": [],
        }, data)
        report = assess(client)
        self.assertEqual({row["component"] for row in report["checks"]}, {"Actions", "Webhooks"})
        self.assertNotIn("API Requests", {row["component"] for row in report["checks"]})


if __name__ == "__main__":
    unittest.main()
