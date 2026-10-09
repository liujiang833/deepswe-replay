"""Per-command timeout selection for trace replay."""

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("deepswe_replay", Path(__file__).resolve().parents[1] / "replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)
crosslang_spec = importlib.util.spec_from_file_location("crosslang_replay", Path(__file__).resolve().parent / "replay.py")
crosslang_replay = importlib.util.module_from_spec(crosslang_spec)
crosslang_spec.loader.exec_module(crosslang_replay)


class CommandTimeoutTests(unittest.TestCase):
    def test_default_and_shorter_explicit_timeout(self):
        self.assertEqual(replay.command_timeout_s("go build ./...", 30), 30)
        self.assertEqual(replay.command_timeout_s("timeout 28 npm test", 30), 30)

    def test_long_explicit_timeout(self):
        self.assertEqual(replay.command_timeout_s("cd /app && timeout 280 go test ./... | tail -5", 30), 290)
        self.assertEqual(replay.command_timeout_s("timeout -k 5 1.5m true; timeout 20s false", 30), 120)

    def test_go_test_timeout(self):
        self.assertEqual(replay.command_timeout_s("go test -race -timeout 120s . | tail -5", 30), 130)
        self.assertEqual(replay.command_timeout_s("timeout 280 go test -timeout=120s .", 30), 410)

    def test_sleep_allows_following_command_to_run(self):
        self.assertEqual(replay.command_timeout_s("sleep 120 && tail -40 /tmp/check.log", 30), 150)
        self.assertEqual(replay.command_timeout_s("sleep 28; tail -5 /tmp/check.log", 30), 58)
        self.assertEqual(replay.command_timeout_s("sleep 5 && echo done", 30), 35)
        self.assertEqual(replay.command_timeout_s("sleep 1m 30s&&echo done", 30), 120)

    def test_sequential_sleep_and_wrapped_sleep(self):
        self.assertEqual(replay.command_timeout_s("sleep 25\nsleep 28 && echo done", 30), 83)
        self.assertEqual(replay.command_timeout_s("sleep 25 && timeout 120 sh -c true", 30), 155)
        self.assertEqual(replay.command_timeout_s("timeout 120 sleep 120", 30), 130)
        self.assertEqual(replay.command_timeout_s("(sleep 120) && echo done", 30), 150)

    def test_bundle_replay_uses_same_sleep_policy(self):
        for command in ("sleep 120 && echo done", "sleep 25 && timeout 120 true", "timeout 120 sleep 120"):
            self.assertEqual(crosslang_replay.command_timeout_s(command, 30),
                             replay.command_timeout_s(command, 30))

    def test_quoted_text_and_heredoc_are_not_commands(self):
        self.assertEqual(replay.command_timeout_s("echo 'timeout 600 go test'", 30), 30)
        self.assertEqual(replay.command_timeout_s("echo 'sleep 120'", 30), 30)
        self.assertEqual(replay.command_timeout_s("echo sleep 120", 30), 30)
        command = "cat <<'EOF' > test.go\ntimeout 600 go test\nEOF\ntimeout 200 go test ./..."
        self.assertEqual(replay.command_timeout_s(command, 30), 210)
        command = "cat <<'EOF' > notes.txt\nsleep 500\nEOF\nsleep 120 && echo done"
        self.assertEqual(replay.command_timeout_s(command, 30), 150)


if __name__ == "__main__":
    unittest.main()
