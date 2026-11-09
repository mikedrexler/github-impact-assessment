import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_file_launcher_resolves_impact_outside_checkout_without_pythonpath(self):
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        with tempfile.TemporaryDirectory() as outside:
            response = subprocess.run(
                [sys.executable, str(ROOT / "run.py"), "--help"],
                cwd=outside, env=environment, capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("Local read-only GitHub incident impact monitor", response.stdout)
        self.assertIn("--demo", response.stdout)
        self.assertEqual(response.stderr, "")

    def test_module_entry_point_still_works_from_checkout(self):
        response = subprocess.run(
            [sys.executable, "-m", "impact", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("--demo", response.stdout)


if __name__ == "__main__":
    unittest.main()
