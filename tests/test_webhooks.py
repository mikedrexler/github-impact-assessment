import copy
import json
import unittest

from impact.assessment import assess, probe
from impact.client import ProbeError
from tests.test_assessment import COMPONENT, INCIDENT, NOW, REPO, WEBHOOKS, FixtureClient


HOOK = {
    "id": 7, "active": True,
    "config": {"url": "https://private-receiver.invalid/sentinel", "secret": "sentinel-secret"},
    "last_response": {"message": "sentinel-response"},
}
DELIVERY = {
    "id": 17, "delivered_at": "2026-11-09T18:20:00Z", "status_code": 503,
    "status": "sentinel-status", "guid": "sentinel-guid",
}


def webhook_client(hooks=None, deliveries=None):
    return FixtureClient({
        "/hooks?": copy.deepcopy([HOOK] if hooks is None else hooks),
        "/deliveries?": copy.deepcopy([DELIVERY] if deliveries is None else deliveries),
    })


class WebhookTests(unittest.TestCase):
    def test_delivery_failure_is_unknown_and_private_fields_do_not_escape(self):
        client = webhook_client()
        row = probe(client, REPO, WEBHOOKS, INCIDENT, NOW)
        self.assertEqual(row["classification"], "unknown")
        self.assertEqual(row["severity"], "degraded")
        self.assertIn("Incident-time delivery failures", row["reason"])
        self.assertIn("receiver, network and configuration", row["reason"])
        self.assertNotIn("sentinel", json.dumps(row))
        self.assertEqual(client.calls, [
            "/repos/example/repo/hooks?per_page=3&page=1",
            "/repos/example/repo/hooks/7/deliveries?per_page=30",
        ])
        self.assertTrue(all(item["url"] == REPO["url"] + "/settings/hooks" for item in row["evidence"]))

    def test_success_empty_historical_future_and_unknown_start_never_mean_unaffected(self):
        for deliveries, incident in [
            ([{**DELIVERY, "status_code": 200}], INCIDENT),
            ([], INCIDENT),
            ([{**DELIVERY, "delivered_at": "2026-11-09T18:09:59Z"}], INCIDENT),
            ([{**DELIVERY, "delivered_at": "2026-11-09T18:50:01Z"}], INCIDENT),
            ([DELIVERY], {"id": "missing-time"}),
        ]:
            with self.subTest(deliveries=deliveries, incident=incident):
                row = probe(webhook_client(deliveries=deliveries), REPO, WEBHOOKS, incident, NOW)
                self.assertEqual(row["classification"], "unknown")
                self.assertEqual(row["severity"], "none")
                self.assertNotIn("delivery failures observed", row["reason"])
                self.assertNotIn("unaffected", json.dumps(row))

    def test_incident_start_boundary_and_no_response_attempt(self):
        row = probe(webhook_client(deliveries=[
            {**DELIVERY, "delivered_at": INCIDENT["started_at"], "status_code": 0},
            {**DELIVERY, "delivered_at": NOW.isoformat(), "status_code": 200},
        ]), REPO, WEBHOOKS, INCIDENT, NOW)
        self.assertTrue(any("2 incident-time attempts, 1 failed/no-response" in item["summary"] for item in row["evidence"]))

    def test_empty_or_inactive_hooks_are_unknown_not_not_applicable(self):
        for hooks in [[], [{**HOOK, "active": False}]]:
            client = webhook_client(hooks=hooks)
            row = probe(client, REPO, WEBHOOKS, INCIDENT, NOW)
            self.assertEqual(row["classification"], "unknown")
            self.assertEqual(len(client.calls), 1)

    def test_bounded_hooks_deliveries_and_explicit_partial_coverage(self):
        hooks = [{**HOOK, "id": i} for i in range(1, 4)]
        client = webhook_client(hooks=hooks, deliveries=[DELIVERY] * 30)
        row = probe(client, REPO, WEBHOOKS, INCIDENT, NOW)
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(any("Hook coverage may be partial" in item["summary"] for item in row["evidence"]))
        self.assertEqual(sum("first page only" in item["summary"] for item in row["evidence"]), 4)

    def test_permission_rate_and_service_errors_are_explicit_unknowns(self):
        for endpoint in ["/hooks?", "/deliveries?"]:
            for kind in ["authentication", "permission_or_secondary_limit", "not_found_or_hidden", "rate_limit", "deadline", "service_error"]:
                client = webhook_client()
                client.responses[endpoint] = ProbeError(kind, "Cannot read webhook evidence")
                row = probe(client, REPO, WEBHOOKS, INCIDENT, NOW)
                self.assertEqual(row["classification"], "unknown")
                self.assertEqual(row["error_kind"], kind)
                self.assertEqual(row["severity"], "none")
                self.assertIsNotNone(row["checked_at"])

    def test_malformed_hook_ids_cannot_construct_paths(self):
        for hook in [None, {**HOOK, "id": "../secrets"}, {**HOOK, "id": True},
                     {**HOOK, "id": -1}, {**HOOK, "active": "false"}]:
            client = webhook_client(hooks=[hook])
            row = probe(client, REPO, WEBHOOKS, INCIDENT, NOW)
            self.assertEqual(row["error_kind"], "invalid_response")
            self.assertEqual(len(client.calls), 1)

    def test_degradation_signal_survives_later_permission_gap_without_becoming_outage(self):
        client = webhook_client(hooks=[HOOK, {**HOOK, "id": 8}])
        client.responses = {
            "/hooks?": [HOOK, {**HOOK, "id": 8}],
            "/hooks/7/deliveries?": [DELIVERY],
            "/hooks/8/deliveries?": ProbeError("permission_or_secondary_limit", "Cannot read"),
        }
        row = probe(client, REPO, WEBHOOKS, INCIDENT, NOW)
        self.assertEqual(row["classification"], "unknown")
        self.assertEqual(row["error_kind"], "permission_or_secondary_limit")
        self.assertEqual(row["severity"], "degraded")
        self.assertTrue(any("1 failed/no-response" in item["summary"] for item in row["evidence"]))

    def test_malformed_or_oversized_samples_remain_unknown(self):
        for endpoint, value in [
            ("/hooks?", [HOOK] * 4),
            ("/deliveries?", [DELIVERY] * 31),
            ("/deliveries?", {"request": "sentinel"}),
            ("/deliveries?", [{**DELIVERY, "status_code": True}]),
            ("/deliveries?", [{**DELIVERY, "status_code": "503"}]),
            ("/deliveries?", [{**DELIVERY, "status_code": 999}]),
            ("/deliveries?", [{**DELIVERY, "delivered_at": "bad"}]),
        ]:
            with self.subTest(endpoint=endpoint, value=value):
                client = webhook_client()
                client.responses[endpoint] = value
                row = probe(client, REPO, WEBHOOKS, INCIDENT, NOW)
                self.assertEqual(row["classification"], "unknown")
                self.assertEqual(row["error_kind"], "invalid_response")
                self.assertNotIn("sentinel", json.dumps(row))

    def test_webhook_only_incident_uses_live_ids_without_actions_or_other_probes(self):
        client = webhook_client()
        client.status_data = {
            "components": [{**COMPONENT, "status": "operational"}, WEBHOOKS,
                           {"id": "pages", "name": "Pages"}],
            "incidents": [{**INCIDENT, "name": "Mixed service title", "components": [{"id": "webhooks"}, {"id": "pages"}]}],
        }
        report = assess(client, inventory_report={"repositories": [REPO], "complete": True})
        self.assertEqual([row["adapter"] for row in report["checks"]], ["webhooks"])
        self.assertEqual(report["incidents"][0]["components"], ["webhooks"])
        self.assertEqual(report["errors"], [])
        self.assertTrue(all("/hooks" in path for path in client.calls))

    def test_resolved_or_wrong_id_incidents_never_probe_webhooks(self):
        for incident in [
            {**INCIDENT, "status": "resolved", "components": [WEBHOOKS]},
            {**INCIDENT, "name": "Webhooks incident", "components": [{"id": "other"}]},
        ]:
            client = webhook_client()
            client.status_data = {"components": [COMPONENT, WEBHOOKS], "incidents": [incident]}
            report = assess(client, inventory_report={"repositories": [REPO], "complete": True})
            self.assertEqual(report["checks"], [])
            self.assertEqual(client.calls, [])

    def test_missing_and_degraded_webhook_component_are_explicit_gaps(self):
        for components, kind in [
            ([COMPONENT], "webhooks_component_missing"),
            ([{**COMPONENT, "status": "operational"}, {**WEBHOOKS, "status": "major_outage"}], "component_without_incident"),
        ]:
            report = assess(FixtureClient(status={"components": components, "incidents": []}),
                            inventory_report={"repositories": [REPO], "complete": True})
            self.assertIn(kind, [error["kind"] for error in report["errors"]])
            self.assertFalse(report["coverage"]["complete"])
            self.assertEqual(report["checks"], [])


if __name__ == "__main__":
    unittest.main()
