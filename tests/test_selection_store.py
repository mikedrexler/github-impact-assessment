import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from impact.app import main
from impact.client import Config
from impact.demo import DemoClient
from impact.monitor import Monitor
from impact.selection_store import SelectionStore


class SelectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SelectionStore(Path(self.directory.name) / "settings/repositories.json")

    def test_roundtrip_selected_paused_and_explicit_all(self):
        self.assertEqual(self.store.load(200), ())
        for mode, names, expected in [
            ("selected", ["Org/Repo", "org/repo", "org/other"], ("Org/Repo", "org/other")),
            ("paused", [], ()), ("all", [], None),
        ]:
            self.store.save(mode, names)
            self.assertEqual(SelectionStore(self.store.path).load(200), expected)
        if os.name == "posix":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_malformed_and_oversized_saved_scope_fails_closed(self):
        self.store.path.parent.mkdir()
        for data in [
            b"not-json", b"\xff", b"[]" , b"x" * 49153,
            json.dumps({"mode": "all", "repositories": ["org/repo"]}).encode(),
            json.dumps({"mode": "invalid", "repositories": []}).encode(),
            json.dumps({"mode": "selected", "repositories": []}).encode(),
            json.dumps({"mode": "selected", "repositories": ["../repo"]}).encode(),
            json.dumps({"mode": "selected", "repositories": ["org/a", "org/b"]}).encode(),
            json.dumps({"mode": "paused", "repositories": [], "token": "secret"}).encode(),
        ]:
            with self.subTest(data=data):
                self.store.path.write_bytes(data)
                with self.assertRaises((ValueError, UnicodeDecodeError)):
                    self.store.load(1)

    def test_atomic_save_failure_preserves_previous_file_and_cleans_temporary(self):
        self.store.save("selected", ["org/old"])
        with patch("impact.selection_store.os.replace", side_effect=OSError("private-path")):
            with self.assertRaises(OSError):
                self.store.save("selected", ["org/new"])
        self.assertEqual(self.store.load(200), ("org/old",))
        self.assertEqual(list(self.store.path.parent.iterdir()), [self.store.path])

    def test_applied_scope_restored_before_first_discovery_without_account_crawl(self):
        calls = []

        class TrackedDemo(DemoClient):
            def get(self, path):
                calls.append(path)
                return super().get(path)

        first = Monitor(Config(), client_factory=TrackedDemo, selected=(), selection_store=self.store)
        first.select_repositories("selected", ["example/queue-symptom"])
        self.assertEqual(self.store.load(200), ("example/queue-symptom",))
        # A queued, accepted scope is durable even before the batch boundary.
        second = Monitor(Config(), client_factory=TrackedDemo, selected=self.store.load(200),
                         selection_store=self.store)
        second._cycle()
        self.assertTrue(calls)
        self.assertTrue(all(path.startswith("/repos/example/queue-symptom") for path in calls))
        self.assertTrue(second.snapshot()["selection"]["persistent"])
        self.assertFalse(second.snapshot()["selection"]["discovery_opted_in"])

    def test_save_failure_keeps_active_and_pending_scope_unchanged(self):
        monitor = Monitor(Config(), selected=("org/old",), selection_store=self.store)
        with patch.object(self.store, "save", side_effect=OSError("secret")):
            with self.assertRaises(OSError):
                monitor.select_repositories("all", [])
        state = monitor.snapshot()["selection"]
        self.assertEqual(state["repositories"], ["org/old"])
        self.assertIsNone(state["pending"])
        self.assertFalse(state["discovery_opted_in"])

    def test_demo_never_writes_even_with_injected_store(self):
        monitor = Monitor(Config(), demo=True, client_factory=DemoClient, selected=(),
                          selection_store=self.store)
        with patch.object(self.store, "save") as save:
            monitor.select_repositories("selected", ["example/queue-symptom"])
            save.assert_not_called()
        self.assertFalse(monitor.snapshot()["selection"]["persistent"])

    def test_startup_restores_saved_scope_cli_overrides_and_demo_ignores(self):
        self.store.save("selected", ["org/saved"])
        for args, selected in [
            ([], ("org/saved",)), (["--repos", "org/override"], ("org/override",)),
            (["--all-repositories"], None), (["--demo"], None),
        ]:
            with self.subTest(args=args), patch("impact.app.SelectionStore", return_value=self.store), patch(
                "sys.argv", ["impact", *args],
            ), patch("impact.app.Monitor") as monitor, patch(
                "impact.app.make_server", side_effect=OSError,
            ), patch("impact.app.sys.stderr"):
                self.assertEqual(main(), 1)
                self.assertEqual(monitor.call_args.kwargs["selected"], selected)
                self.assertIs(monitor.call_args.kwargs["selection_store"], None if "--demo" in args else self.store)
        self.assertEqual(self.store.load(200), ("org/saved",))

    def test_corrupt_saved_scope_blocks_startup_but_explicit_flags_recover(self):
        self.store.path.parent.mkdir()
        self.store.path.write_text("secret-invalid-data")
        with patch("impact.app.SelectionStore", return_value=self.store), patch(
            "sys.argv", ["impact"],
        ), patch("impact.app.Monitor") as monitor, patch("impact.app.sys.stderr") as stderr:
            with self.assertRaises(SystemExit):
                main()
            monitor.assert_not_called()
            self.assertNotIn("secret-invalid-data", str(stderr.mock_calls))
        with patch("impact.app.SelectionStore", return_value=self.store), patch(
            "sys.argv", ["impact", "--repos", "org/recovery"],
        ), patch("impact.app.Monitor") as monitor, patch(
            "impact.app.make_server", side_effect=OSError,
        ), patch("impact.app.sys.stderr"):
            self.assertEqual(main(), 1)
            self.assertEqual(monitor.call_args.kwargs["selected"], ("org/recovery",))
