import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import patch

from impact.client import API_ROOT, STATUS_URL, Backoff, Client, Config, InventoryState, ProbeError, credentials, inventory


class Response:
    def __init__(self, value, headers=None):
        self.data = json.dumps(value).encode()
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.data[:size]


class Opener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class NoWait:
    def is_set(self):
        return False

    def wait(self, seconds):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError(
        API_ROOT + "/private", code, "SECRET error text",
        headers or {}, io.BytesIO(b"SECRET body"),
    )


class ClientTests(unittest.TestCase):
    def client(self, opener, **kwargs):
        return Client("test-secret", Config(**kwargs), NoWait(), opener=opener)

    def test_only_github_receives_token_and_requests_are_get(self):
        opener = Opener(Response({"components": [], "incidents": []}), Response([]))
        client = self.client(opener)
        client.status()
        client.get("/user/repos")
        first, second = [call[0] for call in opener.calls]
        self.assertEqual(first.full_url, STATUS_URL)
        self.assertIsNone(first.get_header("Authorization"))
        self.assertEqual(second.full_url, API_ROOT + "/user/repos")
        self.assertEqual(second.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual([first.get_method(), second.get_method()], ["GET", "GET"])
        self.assertTrue(all(call[1] <= 10 for call in opener.calls))

    def test_absolute_or_protocol_relative_paths_are_rejected(self):
        opener = Opener()
        client = self.client(opener)
        for path in ["https://evil.example/", "//evil.example/", "/bad\\path", "/bad#path"]:
            with self.subTest(path=path), self.assertRaises(ProbeError):
                client.get(path)
        self.assertEqual(opener.calls, [])

    def test_authentication_fails_without_network(self):
        opener = Opener()
        client = Client(None, Config(), threading.Event(), opener=opener)
        with self.assertRaises(ProbeError) as caught:
            client.get("/user")
        self.assertEqual(caught.exception.kind, "authentication")
        self.assertEqual(client.requests, 0)
        self.assertFalse(opener.calls)

    def test_rate_limit_backoff_shared_across_cycles(self):
        backoff = Backoff()
        opener = Opener(http_error(429, {"Retry-After": "120"}))
        first = Client("test-secret", Config(), NoWait(), opener=opener, backoff=backoff, clock=lambda: 10)
        with self.assertRaises(ProbeError) as caught:
            first.get("/user")
        self.assertEqual(caught.exception.kind, "rate_limit")
        second = Client("test-secret", Config(), NoWait(), opener=opener, backoff=backoff, clock=lambda: 20)
        with self.assertRaises(ProbeError) as caught:
            second.get("/user")
        self.assertEqual(caught.exception.kind, "rate_limit")
        self.assertEqual(len(opener.calls), 1)

    def test_permission_and_primary_limit_are_distinct(self):
        for code, headers, kind in [
            (401, {}, "authentication"),
            (403, {}, "permission_or_secondary_limit"),
            (403, {"X-RateLimit-Remaining": "0"}, "rate_limit"),
            (404, {}, "not_found_or_hidden"),
            (302, {"Location": "https://evil.example"}, "service_error"),
        ]:
            with self.subTest(code=code, headers=headers):
                opener = Opener(http_error(code, headers))
                with self.assertRaises(ProbeError) as caught:
                    self.client(opener).get("/user")
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(len(opener.calls), 1)

    def test_public_status_errors_do_not_block_github_inventory(self):
        for code in [403, 429]:
            with self.subTest(code=code):
                client = self.client(Opener(http_error(code), Response([])))
                with self.assertRaises(ProbeError):
                    client.status()
                self.assertEqual(client.get("/user/repos"), [])

    def test_server_errors_retry_at_most_twice(self):
        opener = Opener(http_error(503), http_error(502), Response({"ok": True}))
        self.assertEqual(self.client(opener).get("/user"), {"ok": True})
        self.assertEqual(len(opener.calls), 3)
        opener = Opener(*[http_error(503) for _ in range(3)])
        with self.assertRaises(ProbeError) as caught:
            self.client(opener).get("/user")
        self.assertEqual(caught.exception.kind, "service_error")
        self.assertEqual(len(opener.calls), 3)

    def test_network_failure_retried_and_sanitized(self):
        opener = Opener(*[urllib.error.URLError("SECRET") for _ in range(3)])
        with self.assertRaises(ProbeError) as caught:
            self.client(opener).get("/user")
        self.assertEqual(caught.exception.kind, "network")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(len(opener.calls), 3)

    def test_budget_deadline_cancellation_and_response_limit(self):
        opener = Opener(Response([]))
        client = self.client(opener, max_requests=1)
        client.get("/user")
        with self.assertRaises(ProbeError) as caught:
            client.get("/user")
        self.assertEqual(caught.exception.kind, "request_limit")
        client.deadline = 0
        with self.assertRaises(ProbeError) as caught:
            client.get("/user")
        self.assertEqual(caught.exception.kind, "deadline")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(ProbeError) as caught:
            Client("test-secret", Config(), stop, opener=opener).get("/user")
        self.assertEqual(caught.exception.kind, "cancelled")
        with patch("impact.client.MAX_RESPONSE_BYTES", 2):
            with self.assertRaises(ProbeError) as caught:
                self.client(Opener(Response({"large": "response"}))).get("/user")
        self.assertEqual(caught.exception.kind, "response_limit")

    def test_invalid_json_and_status_shape(self):
        response = Response(None)
        response.data = b"not-json"
        with self.assertRaises(ProbeError) as caught:
            self.client(Opener(response)).get("/user")
        self.assertEqual(caught.exception.kind, "invalid_response")
        for data in [{}, {"components": [1], "incidents": []}, {"components": [], "incidents": [None]}]:
            with self.subTest(data=data), self.assertRaises(ProbeError):
                self.client(Opener(Response(data))).status()

    def test_inventory_paginates_and_deduplicates(self):
        rows = [{"full_name": f"example/r{i}"} for i in range(100)]
        opener = Opener(Response(rows), Response([rows[0], {"full_name": "example/final"}]))
        result = inventory(self.client(opener))
        self.assertEqual(result["discovered"], 101)
        self.assertTrue(result["complete"])
        self.assertIn("page=2", opener.calls[1][0].full_url)
        self.assertIn("organization_member", opener.calls[0][0].full_url)

    def test_exact_full_page_needs_next_page(self):
        rows = [{"full_name": f"example/r{i}"} for i in range(100)]
        result = inventory(self.client(Opener(Response(rows), Response([])), max_repositories=100))
        self.assertTrue(result["complete"])
        result = inventory(self.client(Opener(Response(rows), Response([{"full_name": "example/extra"}])), max_repositories=100))
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"]["kind"], "inventory_limit")

    def test_partial_inventory_keeps_prior_pages(self):
        rows = [{"full_name": f"example/r{i}"} for i in range(100)]
        result = inventory(self.client(Opener(Response(rows), http_error(403))))
        self.assertEqual(result["discovered"], 100)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"]["kind"], "permission_or_secondary_limit")

    def test_inventory_deadline_reserves_probe_time(self):
        opener = Opener()
        result = inventory(self.client(opener), deadline=0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"]["kind"], "inventory_deadline")
        self.assertFalse(opener.calls)

    def test_inventory_request_limit_reserves_probes(self):
        rows = [{"full_name": f"example/r{i}"} for i in range(100)]
        client = self.client(Opener(Response(rows)))
        result = inventory(client, request_limit=1)
        self.assertEqual(result["discovered"], 100)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"]["kind"], "inventory_request_limit")
        # The reserved request remains usable by a subsequent probe.
        self.assertLess(client.requests, client.config.max_requests)

    def test_inventory_resumes_across_bounded_cycles_and_then_revalidates(self):
        state = InventoryState()
        first_page = [{"full_name": f"example/r{i:03}"} for i in range(100)]
        first = inventory(self.client(Opener(Response(first_page))), state=state, request_limit=1)
        self.assertFalse(first["complete"])
        self.assertEqual(first["next_page"], 2)
        self.assertEqual(first["scan_discovered"], 100)
        second_opener = Opener(Response([{"full_name": "example/final"}]))
        second = inventory(self.client(second_opener), state=state, request_limit=1)
        self.assertIn("page=2&", second_opener.calls[0][0].full_url)
        self.assertEqual(second["discovered"], 101)
        self.assertTrue(second["complete"])
        self.assertIsNone(second["next_page"])
        self.assertIsNotNone(second["last_completed_at"])
        next_opener = Opener(Response([]))
        third = inventory(self.client(next_opener), state=state)
        self.assertIn("page=1&", next_opener.calls[0][0].full_url)
        self.assertTrue(third["complete"])
        self.assertEqual(third["discovered"], 0)

    def test_deadline_pause_resumes_at_same_page(self):
        state = InventoryState()
        inventory(self.client(Opener()), state=state, deadline=0)
        self.assertEqual(state.next_page, 1)
        opener = Opener(Response([]))
        self.assertTrue(inventory(self.client(opener), state=state)["complete"])
        self.assertIn("page=1&", opener.calls[0][0].full_url)

    def test_error_retries_failed_page_without_losing_progress(self):
        state = InventoryState()
        rows = [{"full_name": f"example/r{i}"} for i in range(100)]
        first = inventory(self.client(Opener(Response(rows), http_error(429))), state=state)
        self.assertEqual(first["reason"]["kind"], "rate_limit")
        self.assertEqual(first["next_page"], 2)
        opener = Opener(Response([rows[0], {"full_name": "example/final"}]))
        second = inventory(self.client(opener), state=state)
        self.assertTrue(second["complete"])
        self.assertEqual(second["discovered"], 101)
        self.assertIn("page=2&", opener.calls[0][0].full_url)

    def test_cached_inventory_retained_until_complete_refresh(self):
        state = InventoryState()
        inventory(self.client(Opener(Response([{"full_name": "example/removed"}]))), state=state)
        original_completion = state.completed_at
        rows = [{"full_name": f"example/new{i}"} for i in range(100)]
        partial = inventory(self.client(Opener(Response(rows))), state=state, request_limit=1)
        self.assertEqual(partial["discovered"], 101)
        self.assertEqual(partial["cached_repositories"], 1)
        self.assertEqual(partial["last_completed_at"], original_completion)
        self.assertFalse(partial["complete"])
        complete = inventory(self.client(Opener(Response([]))), state=state)
        self.assertEqual(complete["discovered"], 100)
        self.assertFalse(any(repo["name"] == "example/removed" for repo in complete["repositories"]))
        self.assertEqual(complete["cached_repositories"], 0)

    def test_sso_restriction_remembered_across_resume_and_does_not_prune(self):
        state = InventoryState()
        inventory(self.client(Opener(Response([{"full_name": "example/previous"}]))), state=state)
        previous_completion = state.completed_at
        rows = [{"full_name": f"example/new{i}"} for i in range(100)]
        partial = inventory(self.client(Opener(Response(rows, {"X-GitHub-SSO": "partial-results"}))),
                            state=state, request_limit=1)
        self.assertFalse(partial["complete"])
        end = inventory(self.client(Opener(Response([]))), state=state)
        self.assertFalse(end["complete"])
        self.assertEqual(end["reason"]["kind"], "sso_partial_results")
        self.assertEqual(end["last_completed_at"], previous_completion)
        self.assertTrue(any(repo["name"] == "example/previous" for repo in end["repositories"]))
        self.assertIsNone(end["next_page"])
        next_pass = inventory(self.client(Opener()), state=state, deadline=0)
        self.assertEqual(next_pass["discovered"], 101)
        self.assertEqual(next_pass["cached_repositories"], 101)
        self.assertEqual(next_pass["last_completed_at"], previous_completion)

    def test_capacity_limit_does_not_skip_page_tail(self):
        state = InventoryState()
        rows = [{"full_name": f"example/r{i}"} for i in range(100)]
        partial = inventory(self.client(Opener(Response(rows)), max_repositories=50), state=state)
        self.assertEqual(partial["discovered"], 50)
        self.assertEqual(partial["reason"]["kind"], "inventory_limit")
        self.assertEqual(partial["next_page"], 1)
        opener = Opener(Response(rows), Response([]))
        complete = inventory(self.client(opener, max_repositories=200), state=state)
        self.assertEqual(complete["discovered"], 100)
        self.assertTrue(complete["complete"])
        self.assertIn("page=1&", opener.calls[0][0].full_url)

    def test_malformed_page_does_not_commit_or_advance(self):
        state = InventoryState()
        bad = inventory(self.client(Opener(Response([{"full_name": "example/valid"}, None]))), state=state)
        self.assertFalse(bad["complete"])
        self.assertEqual(bad["discovered"], 0)
        self.assertEqual(bad["next_page"], 1)

    def test_sso_partial_header_is_not_a_complete_inventory(self):
        response = Response([{"full_name": "example/visible"}], {
            "X-GitHub-SSO": "partial-results; organizations=1234",
        })
        result = inventory(self.client(Opener(response)))
        self.assertEqual(result["discovered"], 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"]["kind"], "sso_partial_results")
        self.assertNotIn("1234", json.dumps(result))

    def test_unknown_feature_flags_do_not_become_disabled(self):
        result = inventory(self.client(Opener(Response([{"full_name": "example/visible"}]))))
        self.assertIsNone(result["repositories"][0]["has_issues"])
        self.assertIsNone(result["repositories"][0]["has_pages"])

    def test_invalid_inventory_cannot_inject_urls(self):
        result = inventory(self.client(Opener(Response([{"full_name": "evil.example/?token=foo"}]))))
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"]["kind"], "invalid_response")
        self.assertEqual(result["repositories"], [])

    @patch.dict("os.environ", {"GH_TOKEN": "sentinel-private-token"}, clear=True)
    @patch("impact.client.subprocess.run")
    def test_env_credentials_do_not_spawn_gh(self, run):
        self.assertEqual(credentials(), ("sentinel-private-token", "environment"))
        run.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("impact.client.subprocess.run")
    def test_gh_credentials_fixed_command_and_timeout(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "sentinel-private-token\n"
        self.assertEqual(credentials(), ("sentinel-private-token", "gh authentication"))
        self.assertEqual(run.call_args.args[0], ["gh", "auth", "token", "--hostname", "github.com"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch.dict("os.environ", {"GH_TOKEN": "bad\nheader"}, clear=True)
    def test_invalid_env_token_not_used(self):
        token, source = credentials()
        self.assertIsNone(token)
        self.assertNotIn("bad", source)

    def test_config_rejects_unbounded_values(self):
        for value in ["0", "999", "invalid"]:
            with self.subTest(value=value), patch.dict("os.environ", {"IMPACT_WORKERS": value}):
                with self.assertRaises(ValueError):
                    Config.from_env()

    def test_discovery_config_is_bounded_and_parsed(self):
        for key in ["DISCOVERY_SECONDS", "DISCOVERY_REQUESTS", "DISCOVERY_RESUME_SECONDS"]:
            with self.subTest(key=key), patch.dict("os.environ", {f"IMPACT_{key}": "0"}):
                with self.assertRaises(ValueError):
                    Config.from_env()
        with patch.dict("os.environ", {
            "IMPACT_DISCOVERY_SECONDS": "7", "IMPACT_DISCOVERY_REQUESTS": "3",
            "IMPACT_DISCOVERY_RESUME_SECONDS": "9",
        }):
            config = Config.from_env()
        self.assertEqual((config.discovery_seconds, config.discovery_requests, config.discovery_resume_seconds), (7, 3, 9))


if __name__ == "__main__":
    unittest.main()
