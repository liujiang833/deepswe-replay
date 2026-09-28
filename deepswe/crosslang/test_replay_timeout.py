"""Per-command timeout selection for trace replay."""

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("deepswe_replay", Path(__file__).resolve().parents[1] / "replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


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

    def test_quoted_text_and_heredoc_are_not_commands(self):
        self.assertEqual(replay.command_timeout_s("echo 'timeout 600 go test'", 30), 30)
        command = "cat <<'EOF' > test.go\ntimeout 600 go test\nEOF\ntimeout 200 go test ./..."
        self.assertEqual(replay.command_timeout_s(command, 30), 210)


if __name__ == "__main__":
    unittest.main()
