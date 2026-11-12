import copy
import json
import threading
import unittest
from unittest.mock import patch

from impact.assessment import assess
from impact.client import INCIDENTS_URL, Client, Config, ProbeError
from impact.demo import DemoClient
from impact.history import project_incidents
from impact.monitor import Monitor
from tests.test_client import Opener, Response


def incident(identity="past", started="2026-11-09T18:12:37Z", **extra):
    return {
        "id": identity, "name": identity, "status": "resolved", "started_at": started,
        "resolved_at": "2026-11-09T18:59:32Z", "components": [{"id": "actions", "name": "Actions"}],
        **extra,
    }


class HistoryTests(unittest.TestCase):
    def test_past_history_includes_webhooks_and_excludes_other_services(self):
        rows = project_incidents({"incidents": [
            incident("pages", components=[{"id": "pages", "name": "Pages"}]),
            incident("mixed", components=[{"id": "pages", "name": "Pages"}, {"id": "actions", "name": "Actions"}]),
            incident("hooks", components=[{"id": "webhooks", "name": "Webhooks"}]),
        ]})
        self.assertEqual([item["id"] for item in rows], ["mixed", "hooks"])
        self.assertEqual(rows[0]["components"], ["Actions"])
        self.assertEqual(rows[1]["components"], ["Webhooks"])

    def test_public_incident_impact_is_retained_separately_from_tenant_classification(self):
        rows = project_incidents({"incidents": [incident(impact="major")]})
        self.assertEqual(rows[0]["impact"], "major")
        self.assertNotIn("classification", rows[0])

    def test_public_history_never_receives_credentials(self):
        opener = Opener(Response({"incidents": [incident()]}))
        client = Client("sentinel-private-token", Config(), threading.Event(), opener=opener)
        self.assertEqual(len(client.past_incidents()["incidents"]), 1)
        request = opener.calls[0][0]
        self.assertEqual(request.full_url, INCIDENTS_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))
        with self.assertRaises(ProbeError):
            Client(None, Config(max_requests=1), threading.Event(), opener=Opener(Response([]))).past_incidents()

    def test_date_order_uses_instants_not_strings_or_resolution(self):
        rows = project_incidents({"incidents": [
            incident("older", "2026-11-08T23:00:00Z"),
            incident("newer", "2026-11-09T12:12:37-06:00"),
            incident("middle", "2026-11-09T18:00:00Z"),
            incident("invalid", "invalid"),
            incident("active", status="investigating"),
            incident("newer"),
        ]})
        self.assertEqual([row["id"] for row in rows], ["newer", "middle", "older", "invalid"])
        self.assertEqual(rows[0]["url"], "https://www.githubstatus.com/incidents/newer")

    def test_bounds_and_timeline_projection(self):
        updates = [{"created_at": "2026-11-09T19:00:00Z", "body": "<script>unsafe</script>"},
                   {"display_at": "2026-11-09T18:00:00Z", "body": "earlier"}]
        rows = project_incidents({"incidents": [incident(incident_updates=updates, private_field="excluded")]})
        self.assertEqual(rows[0]["updates"][0]["body"], "earlier")
        self.assertNotIn("private_field", rows[0])
        rows = project_incidents({"incidents": [
            incident(str(i), incident_updates=updates * 60) for i in range(60)
        ]})
        self.assertEqual(len(rows), 50)
        self.assertEqual(len(rows[0]["updates"]), 100)
        self.assertTrue(rows[0]["timeline_partial"])

    def test_invalid_payloads_reject_atomically(self):
        for data in [{"incidents": None}, {"incidents": [None]},
                     {"incidents": [incident(), incident("../bad")]},
                     {"incidents": [incident(incident_updates=[None])]}]:
            with self.subTest(data=data), self.assertRaises(ProbeError):
                project_incidents(data)

    def test_retains_observed_evidence_after_resolution_and_deduplicates(self):
        monitor = Monitor(Config(), client_factory=DemoClient, demo=True)
        monitor._cycle()
        first = monitor.past_snapshot()
        self.assertEqual(first["incidents"][0]["assessment"]["counts"]["suspected"], 1)
        self.assertIsNone(first["incidents"][1]["assessment"])
        original_time = first["incidents"][0]["assessment"]["completed_at"]
        class ResolvedDemo(DemoClient):
            def status(self):
                data = super().status()
                data["incidents"] = []
                return data

            def past_incidents(self):
                return DemoClient(self.config, self.stop).past_incidents()

        monitor._factory = ResolvedDemo
        monitor._cycle()
        later = monitor.past_snapshot()
        self.assertEqual(later["incidents"][0]["assessment"]["completed_at"], original_time)
        first["incidents"][0]["assessment"]["checks"].clear()
        self.assertEqual(len(later["incidents"][0]["assessment"]["checks"]), 6)
        self.assertEqual(len(monitor._incident_evidence), 1)
        self.assertEqual(len(monitor.snapshot()["history"]), 2)

    def test_errors_retain_last_success_and_do_not_fail_live_assessment(self):
        monitor = Monitor(Config(), client_factory=DemoClient)
        monitor._cycle()
        before = monitor.past_snapshot()
        with patch.object(DemoClient, "past_incidents", side_effect=ProbeError("request_limit", "Budget exhausted")):
            monitor._cycle()
        after = monitor.past_snapshot()
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(after["error"]["kind"], "request_limit")
        self.assertIsNone(monitor.snapshot()["error"])
        self.assertEqual(len(monitor.snapshot()["history"]), 2)
        with patch.object(DemoClient, "past_incidents", side_effect=RuntimeError("sentinel-secret")), patch("impact.monitor.sys.stderr"):
            monitor._cycle()
        self.assertNotIn("sentinel-secret", json.dumps(monitor.past_snapshot()))
        fresh = Monitor(Config(), client_factory=DemoClient)
        self.assertFalse(fresh.past_snapshot()["available"])
        self.assertEqual(fresh.past_snapshot()["incidents"], [])

    def test_credential_change_clears_private_evidence(self):
        with patch("impact.monitor.credentials", return_value=("old", "environment")) as auth, patch(
            "impact.monitor.Client", side_effect=lambda *args, **kwargs: DemoClient(Config(), threading.Event()),
        ):
            monitor = Monitor(Config())
            monitor._cycle()
            self.assertTrue(monitor._incident_evidence)
            auth.return_value = ("new", "environment")
            monitor._new_client(Config())
            self.assertFalse(monitor._incident_evidence)
            self.assertTrue(all(row["assessment"] is None for row in monitor.past_snapshot()["incidents"]))

    def test_evidence_cache_is_bounded_and_discovery_does_not_update_it(self):
        monitor = Monitor(Config(), client_factory=DemoClient)
        for index in range(12):
            def report(client, *args, **kwargs):
                result = assess(client, *args, **kwargs)
                result["incidents"][0]["id"] = str(index)
                for row in result["checks"]:
                    row["incident_id"] = str(index)
                return result

            with patch("impact.monitor.assess", side_effect=report):
                monitor._cycle()
        self.assertEqual(len(monitor._incident_evidence), 10)
        self.assertNotIn("0", monitor._incident_evidence)
        before = copy.deepcopy(monitor._incident_evidence)
        monitor._discover()
        self.assertEqual(monitor._incident_evidence, before)


if __name__ == "__main__":
    unittest.main()
