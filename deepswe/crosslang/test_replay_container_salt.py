"""Container ownership and naming checks without a Docker daemon."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("replay_salted", Path(__file__).with_name("replay.py"))
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class ContainerSaltTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trial = self.root / "same.trial"
        self.trial.mkdir()
        (self.trial / "trajectory.json").write_text(json.dumps({"steps": []}))
        (self.trial / "task.json").write_text(json.dumps({"files": [{
            "path": "task.toml", "content": 'docker_image = "test:latest"\nbase_commit_hash = "abc"\n',
        }]}))

    def run_replay(self, salt, run_id, collision=False, foreign_owner=False):
        calls = []
        name = f"replay_same.trial_123_{salt}"

        def fake_sh(argv, **_kwargs):
            calls.append(argv)
            if argv[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if argv[:3] == ["docker", "container", "inspect"]:
                return subprocess.CompletedProcess(argv, 0 if collision else 1, b"", b"")
            if argv[:3] == ["docker", "run", "-d"]:
                return subprocess.CompletedProcess(argv, 1, b"", b"simulated startup failure")
            if argv[:3] == ["docker", "inspect", "-f"]:
                label = "someone-else" if foreign_owner else run_id
                return subprocess.CompletedProcess(argv, 0, (label + "\n").encode(), b"")
            if argv[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            raise AssertionError(argv)

        argv = ["replay.py", str(self.trial), str(self.trial / "task.json"),
                "-o", str(self.root / "out"), "--net-mode", "none",
                "--container-salt", salt]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(replay.os, "getpid", return_value=123), \
                mock.patch.object(replay.secrets, "token_hex", return_value=run_id), \
                mock.patch.object(replay, "sh", side_effect=fake_sh), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = replay.main()
        self.assertEqual(rc, 1)
        return name, calls

    def test_two_runs_of_same_trial_use_distinct_names_and_only_clean_own_containers(self):
        first, first_calls = self.run_replay("a" * 12, "1" * 24)
        second, second_calls = self.run_replay("b" * 12, "2" * 24)
        self.assertNotEqual(first, second)
        for name, calls in ((first, first_calls), (second, second_calls)):
            self.assertIn(["docker", "container", "inspect", name], calls)
            self.assertIn(["docker", "rm", "-f", name], calls)
            self.assertIn(["docker", "rm", "-f", name + "-sink"], calls)
            self.assertFalse(any(cmd[:2] == ["docker", "ps"] for cmd in calls))

    def test_exact_name_collision_never_removes_existing_container(self):
        name, calls = self.run_replay("c" * 12, "3" * 24, collision=True)
        self.assertIn(["docker", "container", "inspect", name], calls)
        self.assertFalse(any(cmd[:2] == ["docker", "run"] for cmd in calls))
        self.assertFalse(any(cmd[:2] == ["docker", "rm"] for cmd in calls))

    def test_teardown_skips_containers_with_foreign_owner_label(self):
        _, calls = self.run_replay("d" * 12, "4" * 24, foreign_owner=True)
        self.assertFalse(any(cmd[:2] == ["docker", "rm"] for cmd in calls))


if __name__ == "__main__":
    unittest.main()
