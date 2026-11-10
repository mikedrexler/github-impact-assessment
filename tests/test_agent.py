import copy
import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import patch

from impact.agent import (
    MAX_INPUT_BYTES, MAX_MODEL_CALLS, MAX_OUTPUT_BYTES, MAX_OUTPUT_TOKENS,
    MAX_TOOL_CALLS, MODEL_URL, Agent, OpenAIModel, PlannerError, ProbeContext,
    plan_schema, validate_plan,
)
from impact.assessment import assess, probe
from impact.client import Client, Config, ProbeError
from impact.demo import DemoClient
from impact.monitor import Monitor
from tests.test_assessment import COMPONENT, INCIDENT, NOW, REPO, FixtureClient, action_client
from tests.test_client import Opener, Response

SCOPED_INCIDENT = {**INCIDENT, "status": "investigating", "components": [COMPONENT]}


def plan(operation="actions_runs", repository_id="r1", target_id="none", **kwargs):
    return {
        "operation": operation, "repository_id": repository_id, "target_id": target_id,
        "reason": "inspect_component", "uncertainty": "no_attribution", **kwargs,
    }


class MockModel:
    def __init__(self, *values):
        self.values = iter(values)
        self.calls = []

    def plan(self, context, schema, timeout):
        self.calls.append(copy.deepcopy((context, schema, timeout)))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def context(client=None, component=None, incident=None, repo=None):
    client = client or action_client(jobs=[{
        "id": 2, "status": "queued", "created_at": "2026-11-09T18:20:00Z",
        "labels": ["ubuntu-latest"],
    }])
    client.get_once = lambda path, deadline: client.get(path)
    return ProbeContext(client, repo or REPO, component or COMPONENT, incident or SCOPED_INCIDENT, NOW)


def report_for(model, ctx=None):
    return Agent(model).run([ctx or context()], threading.Event())


def provider_response(value, **kwargs):
    return Response({
        "status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}],
        }], **kwargs,
    })


class AgentTests(unittest.TestCase):
    def test_sequential_followups_use_sanitized_results_and_reuse_reads(self):
        class AdaptiveModel(MockModel):
            def plan(self, data, schema, timeout):
                self.calls.append(copy.deepcopy((data, schema, timeout)))
                if not data["observations"]:
                    return plan()
                last = data["observations"][-1]
                if last["operation"] == "actions_runs":
                    self_test.assertEqual(last["result"]["incident_run_refs"], ["run1"])
                    return plan("actions_jobs", target_id="run1", reason="inspect_queue")
                if last["operation"] == "actions_jobs":
                    self_test.assertEqual(last["result"]["queued_count"], 1)
                    return plan("commit_checks", target_id="run1", reason="inspect_checks")
                return plan("stop", repository_id="none", reason="sufficient_sample")

        self_test = self
        model = AdaptiveModel()
        ctx = context()
        result = report_for(model, ctx)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(result["model_calls"], 3)  # No choices remain; no extra call.
        self.assertEqual(len(ctx.client.calls), 3)
        row = probe(ctx, REPO, COMPONENT, SCOPED_INCIDENT, NOW)
        self.assertEqual(row["classification"], "suspected")
        self.assertEqual(len(ctx.client.calls), 3)
        self.assertTrue(all(call[2] <= 5 for call in model.calls))
        self.assertIn("attribute", result["trace"][0]["uncertainty"])

    def test_invalid_and_malicious_plans_never_execute(self):
        bad_plans = [
            None, [], {}, "run shell", {**plan(), "shell": "curl evil"},
            plan(operation="dispatch_workflow"), plan(repository_id="example/repo"),
            plan(repository_id="https://evil.example"), plan(repository_id="r9"),
            plan(target_id="../secrets"), plan(target_id=True), plan(operation=["actions_runs"]),
            plan(reason="This repository is unaffected"), plan(uncertainty="confirmed"),
            plan("actions_jobs", target_id="run1"), plan("commit_checks", target_id="run1"),
            plan("stop"), plan("repository_metadata"), plan("commit_sample"),
            plan("pull_request_sample"), plan("issue_sample"), plan("pages_configuration"),
            plan(repository_id="none"),
        ]
        for value in bad_plans:
            with self.subTest(value=value):
                ctx = context()
                report = report_for(MockModel(value), ctx)
                self.assertEqual(report["status"], "fallback")
                self.assertIn(report["error"], {"invalid_plan", "disallowed_operation"})
                self.assertEqual(ctx.client.calls, [])
                self.assertNotIn("curl evil", json.dumps(report))

    def test_status_scope_and_feature_flags_cannot_be_overridden(self):
        for component, incident, repo in [
            (COMPONENT, {**SCOPED_INCIDENT, "status": "resolved"}, REPO),
            (COMPONENT, {**SCOPED_INCIDENT, "components": [{"id": "different"}]}, REPO),
            ({"id": "actions", "name": "Webhooks"}, SCOPED_INCIDENT, REPO),
            ({"id": "actions", "name": "Copilot"}, SCOPED_INCIDENT, REPO),
            ({"id": "actions", "name": "Pages"}, SCOPED_INCIDENT, {**REPO, "has_pages": False}),
            ({"id": "actions", "name": "Issues"}, SCOPED_INCIDENT, {**REPO, "has_issues": False}),
        ]:
            model = MockModel(plan())
            ctx = context(component=component, incident=incident, repo=repo)
            self.assertEqual(report_for(model, ctx)["status"], "idle")
            self.assertEqual(model.calls, [])
            with self.assertRaises(PlannerError):
                ctx.execute("actions_runs", "none", 10)
            self.assertEqual(ctx.client.calls, [])

    def test_removed_component_operations_cannot_execute(self):
        for name, operation, path, response in [
            ("API Requests", "repository_metadata", "", {}),
            ("Git Operations", "commit_sample", "/commits?per_page=1&page=1", []),
            ("Pull Requests", "pull_request_sample", "/pulls?state=open&per_page=1&page=1", []),
            ("Issues", "issue_sample", "/issues?state=all&per_page=1&page=1", []),
            ("Pages", "pages_configuration", "/pages", {}),
        ]:
            with self.subTest(component=name):
                ctx = context(FixtureClient({"/repos/example/repo" + path: response}),
                              component={"id": "actions", "name": name})
                report = report_for(MockModel(plan(operation)), ctx)
                self.assertEqual(report["status"], "idle")
                with self.assertRaises(PlannerError):
                    ctx.execute(operation, "none", 10)
                self.assertEqual(probe(ctx, REPO, ctx.component, SCOPED_INCIDENT, NOW)["classification"], "unknown")
                self.assertEqual(ctx.client.calls, [])

    def test_execution_revalidates_status_and_target_ids(self):
        ctx = context()
        ctx.incident = {**SCOPED_INCIDENT, "status": "postmortem"}
        with self.assertRaises(PlannerError):
            ctx.execute("actions_runs", "none", 10)
        ctx = context()
        ctx.runs = {"run1": {"id": True, "head_sha": "a" * 40}}
        with self.assertRaises(PlannerError):
            ctx.execute("actions_jobs", "run1", 10)
        ctx.runs["run1"]["id"] = "../other/repo"
        with self.assertRaises(PlannerError):
            ctx.execute("actions_jobs", "run1", 10)
        ctx.runs["run1"] = {"id": 1, "head_sha": "../../other"}
        ctx.completed.add(("actions_jobs", "run1"))
        with self.assertRaises(PlannerError):
            ctx.execute("commit_checks", "run1", 10)
        self.assertEqual(ctx.client.calls, [])

    def test_only_incident_overlapping_runs_can_be_selected(self):
        client = action_client({
            "id": 1, "created_at": "2026-11-09T17:00:00Z", "updated_at": "2026-11-09T18:00:00Z",
            "status": "completed", "head_sha": "a" * 40,
        })
        ctx = context(client)
        report = report_for(MockModel(plan()), ctx)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(ctx.runs, {})
        self.assertEqual(len(client.calls), 1)

    def test_duplicate_check_is_rejected(self):
        ctx = context()
        report = report_for(MockModel(plan(), plan()), ctx)
        self.assertEqual(report["error"], "disallowed_operation")
        self.assertEqual(len(ctx.client.calls), 1)

    def test_iteration_tool_and_context_limits(self):
        contexts = [context() for _ in range(10)]
        model = MockModel(*(plan(repository_id=f"r{i + 1}") for i in range(4)))
        report = Agent(model).run(contexts, threading.Event())
        self.assertEqual(report["error"], "tool_limit")
        self.assertEqual(report["tool_calls"], MAX_TOOL_CALLS)
        self.assertEqual(report["model_calls"], MAX_MODEL_CALLS)
        self.assertEqual(report["contexts_considered"], 4)
        self.assertEqual(sum(len(ctx.client.calls) for ctx in contexts), 3)
        self.assertEqual(len(model.calls[0][0]["scope"]), 4)

    def test_stop_deadline_cancellation_and_single_flight(self):
        model = MockModel(plan("stop", repository_id="none", reason="sufficient_sample"))
        report = report_for(model)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["tool_calls"], 0)
        agent = Agent(MockModel(plan()), clock=lambda: 0)
        agent._lock.acquire()
        self.assertEqual(agent.run([context()], threading.Event())["error"], "already_running")
        agent._lock.release()
        stop = threading.Event()
        stop.set()
        self.assertEqual(agent.run([context()], stop)["error"], "cancelled")
        clock = iter([0, 0, 0, 0, 21])
        agent = Agent(MockModel(plan()), clock=lambda: next(clock))
        ctx = context()
        self.assertEqual(agent.run([ctx], threading.Event())["error"], "deadline")
        self.assertEqual(ctx.client.calls, [])

    def test_failure_after_partial_execution_keeps_deterministic_verdict(self):
        ctx = context()
        report = report_for(MockModel(plan(), plan("actions_jobs", target_id="run1"), {"invalid": True}), ctx)
        self.assertEqual(report["status"], "fallback")
        row = probe(ctx, REPO, COMPONENT, SCOPED_INCIDENT, NOW)
        self.assertEqual(row["classification"], "suspected")
        self.assertEqual(len(ctx.client.calls), 3)

    def test_no_model_receives_raw_incident_repository_or_probe_text(self):
        secrets = ["secret-repo-name", "SECRET-ISSUE-TITLE", "SECRET-INCIDENT", "SECRET-TOKEN", "SECRET-URL"]
        client = action_client({
            "id": 1, "created_at": "2026-11-09T18:15:00Z", "updated_at": "2026-11-09T18:49:00Z",
            "status": "queued", "head_sha": "a" * 40, "name": secrets[1], "html_url": secrets[4],
            "token": secrets[3], "repository": secrets[0],
        }, jobs=[{"id": 2, "status": "queued", "name": secrets[1], "labels": ["self-hosted", secrets[3]]}])
        model = MockModel(plan(), plan("actions_jobs", target_id="run1"), plan("stop", repository_id="none"))
        report_for(model, context(client, incident={**SCOPED_INCIDENT, "name": secrets[2]},
                                  repo={**REPO, "name": "example/" + secrets[0]}))
        encoded = json.dumps(model.calls)
        for secret in secrets:
            self.assertNotIn(secret, encoded)
        self.assertNotIn("head_sha", encoded)
        self.assertNotIn("html_url", encoded)

    def test_errors_are_sanitized_and_rate_limit_has_cross_cycle_cooldown(self):
        model = MockModel(PlannerError("rate_limit"))
        agent = Agent(model, clock=lambda: 10)
        self.assertEqual(agent.run([context()], threading.Event())["error"], "rate_limit")
        self.assertEqual(agent.run([context()], threading.Event())["error"], "provider_cooldown")
        self.assertEqual(len(model.calls), 1)
        for error in [PlannerError("sentinel-secret"), RuntimeError("sentinel-secret")]:
            report = report_for(MockModel(error))
            self.assertEqual(report["error"], "planner_error")
            self.assertNotIn("sentinel-secret", json.dumps(report))

    def test_invalid_probe_data_is_visible_but_never_sent_raw(self):
        ctx = context(FixtureClient({"/actions/runs?": {"workflow_runs": "secret-malformed"}}))
        model = MockModel(plan())
        report = report_for(model, ctx)
        self.assertEqual(report["trace"][0]["result"]["outcome"], "read_error")
        self.assertNotIn("secret-malformed", json.dumps(model.calls))
        row = probe(ctx, REPO, COMPONENT, SCOPED_INCIDENT, NOW)
        self.assertEqual(row["classification"], "unknown")
        self.assertEqual(row["error_kind"], "invalid_response")

    def test_config_opt_in_and_demo_never_contacts_provider(self):
        for env, status, error in [
            ({}, "disabled", None),
            ({"OPENAI_API_KEY": "secret"}, "disabled", None),
            ({"IMPACT_AI_PROVIDER": "other"}, "fallback", "unsupported_provider"),
            ({"IMPACT_AI_PROVIDER": "openai"}, "fallback", "missing_key"),
            ({"IMPACT_AI_PROVIDER": "openai", "OPENAI_API_KEY": "secret"}, "fallback", "invalid_configuration"),
        ]:
            with self.subTest(env=env), patch.dict("os.environ", env, clear=True):
                report = Agent.from_env().run([context()], threading.Event())
                self.assertEqual((report["status"], report["error"]), (status, error))
        with patch.dict("os.environ", {
            "IMPACT_AI_PROVIDER": "openai", "OPENAI_API_KEY": "secret", "IMPACT_AI_MODEL": "explicit-model",
        }, clear=True):
            self.assertIsInstance(Agent.from_env()._model, OpenAIModel)
            self.assertIsNone(Agent.from_env(demo=True)._model)
            self.assertNotIn("secret", json.dumps(Monitor(Config(), demo=True).snapshot()))

    def test_assessment_fallback_and_no_status_scope_bypass(self):
        for model in [None, MockModel(plan(operation="shell")), MockModel(PlannerError("rate_limit"))]:
            client = FixtureClient({"/user/repos?": [{"full_name": "example/repo"}], "/actions/runs?": {"workflow_runs": []}}, {
                "components": [COMPONENT],
                "incidents": [{**INCIDENT, "components": [COMPONENT]}],
            })
            report = assess(client, agent=Agent(model))
            self.assertEqual(report["checks"][0]["classification"], "unknown")
            self.assertEqual(report["counts"]["confirmed"], 0)
            self.assertEqual(client.calls, [
                "/user/repos?per_page=100&page=1&affiliation=owner,collaborator,organization_member&sort=full_name&direction=asc",
                "/repos/example/repo/actions/runs?per_page=30&page=1",
            ])
        model = MockModel(plan())
        report = assess(FixtureClient({"/user/repos?": []}), agent=Agent(model))
        self.assertEqual(model.calls, [])
        self.assertEqual(report["agent"]["status"], "idle")

    def test_enabled_assessment_matches_baseline_and_reuses_all_reads(self):
        baseline = context().client
        enabled = context().client
        status = {"components": [COMPONENT], "incidents": [SCOPED_INCIDENT]}
        baseline.status_data = enabled.status_data = status
        found = {"repositories": [REPO], "complete": True, "discovered": 1}
        expected = assess(baseline, inventory_report=found)
        model = MockModel(plan(), plan("actions_jobs", target_id="run1"), plan("commit_checks", target_id="run1"))
        actual = assess(enabled, inventory_report=found, agent=Agent(model))
        self.assertEqual(actual["counts"], expected["counts"])
        self.assertEqual(actual["checks"][0]["classification"], "suspected")
        self.assertEqual(actual["checks"][0]["evidence"], expected["checks"][0]["evidence"])
        self.assertEqual(actual["agent"]["status"], "completed")
        self.assertEqual(enabled.calls, baseline.calls)

    def test_injected_fixture_clients_do_not_enable_real_provider(self):
        with patch.dict("os.environ", {
            "IMPACT_AI_PROVIDER": "openai", "OPENAI_API_KEY": "provider-secret", "IMPACT_AI_MODEL": "model",
        }), patch("impact.agent.OpenAIModel.plan") as provider:
            monitor = Monitor(Config(), client_factory=DemoClient)
            monitor._cycle()
            provider.assert_not_called()
            self.assertEqual(monitor.snapshot()["latest"]["agent"]["status"], "disabled")
            model = MockModel(PlannerError("sentinel-secret"))
            monitor = Monitor(Config(), client_factory=DemoClient, agent=Agent(model))
            monitor._cycle()
            snapshot = json.dumps(monitor.snapshot())
            self.assertNotIn("provider-secret", snapshot)
            self.assertNotIn("sentinel-secret", snapshot)
            self.assertEqual(monitor.snapshot()["latest"]["agent"]["status"], "fallback")


class ProviderTests(unittest.TestCase):
    def test_fixed_origin_strict_schema_no_tools_and_secret_boundary(self):
        opener = Opener(provider_response(plan()))
        provider = OpenAIModel("provider-secret", "explicit-model", opener=opener)
        self.assertEqual(provider.plan({"scope": []}, plan_schema(), 3), plan())
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, MODEL_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer provider-secret")
        payload = json.loads(request.data)
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(payload["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["text"]["format"]["schema"]["additionalProperties"])
        self.assertEqual(timeout, 3)
        self.assertNotIn("provider-secret", request.data.decode())

    def test_http_errors_no_retry_and_no_raw_error_body(self):
        for code, expected in [(401, "authentication"), (403, "permission"), (404, "model_unavailable"),
                               (429, "rate_limit"), (500, "provider_http_error"), (302, "provider_http_error")]:
            error = urllib.error.HTTPError(MODEL_URL, code, "sentinel-secret", {}, io.BytesIO(b"sentinel-secret"))
            opener = Opener(error)
            with self.assertRaises(PlannerError) as caught:
                OpenAIModel("key", "model", opener=opener).plan({}, plan_schema(), 1)
            self.assertEqual(str(caught.exception), expected)
            self.assertEqual(len(opener.calls), 1)

    def test_response_schema_refusal_network_and_size_limits(self):
        for response, expected in [
            (Response({"status": "incomplete"}), "incomplete_or_refused"),
            (Response({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]}),
             "incomplete_or_refused"),
            (Response({"status": "completed", "output": None}), "invalid_response"),
            (provider_response(plan(), output=[{"type": "message", "content": None}]), "invalid_response"),
            (Response("x" * MAX_OUTPUT_BYTES), "response_limit"),
            (TimeoutError("secret"), "provider_network"),
            (urllib.error.URLError("secret"), "provider_network"),
        ]:
            with self.subTest(expected=expected), self.assertRaises(PlannerError) as caught:
                OpenAIModel("key", "model", opener=Opener(response)).plan({}, plan_schema(), 1)
            self.assertEqual(str(caught.exception), expected)
        opener = Opener()
        with self.assertRaises(PlannerError):
            OpenAIModel("key", "model", opener=opener).plan({"secret": "x" * MAX_INPUT_BYTES}, plan_schema(), 1)
        self.assertEqual(opener.calls, [])

    def test_planner_github_reads_share_cycle_budget_and_do_not_retry(self):
        error = urllib.error.HTTPError("https://api.github.com/repos/a/b", 503, "secret", {}, io.BytesIO(b"secret"))
        opener = Opener(error, Response({}))
        client = Client("github-secret", Config(max_requests=2), threading.Event(), opener=opener, clock=lambda: 10)
        with self.assertRaises(ProbeError):
            client.get_once("/repos/a/b", 12)
        self.assertEqual(client.requests, 1)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0][1], 2)
        client.get("/repos/a/b")
        self.assertEqual(client.requests, 2)
        with self.assertRaises(ProbeError):
            client.get_once("/repos/a/b", 9)
        self.assertEqual(len(opener.calls), 2)
        for request, _ in opener.calls:
            self.assertEqual(request.get_method(), "GET")
            self.assertNotIn("provider-secret", str(request.headers))


if __name__ == "__main__":
    unittest.main()
