"""The checkpoint diagnosis should identify the failing command without Docker."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagnose_step_prepare as diagnose


class DiagnoseStepPrepareTest(unittest.TestCase):
    def test_report_links_command_step_and_both_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "logs" / "0001" / "trial-a"
            result.mkdir(parents=True)
            (result / "verdict.json").write_text(json.dumps({
                "n_replayed": 2, "rc_match_semantic": "1/2",
                "rc_mismatches": [{"i": 1, "trace_rc": 0, "replay_rc": 127,
                                   "semantic_match": False, "wall_s": 0.2}]}))
            (result / "commands.jsonl").write_text(json.dumps({
                "i": 1, "step": 2, "cmd": "cd /app && missing-tool",
                "stdout_head": "", "stderr_head": "missing-tool: not found"}) + "\n")
            trial = root / "trials" / "trial-a"
            trial.mkdir(parents=True)
            (trial / "trajectory.json").write_text(json.dumps({"steps": [
                {"tool_calls": [{"arguments": {"command": "first"}}],
                 "observation": {"results": [{"content": '{"returncode": 0, "output": "ok"}'}]}},
                {"tool_calls": [{"arguments": {"command": "missing-tool"}}],
                 "observation": {"results": [{"content": '{"returncode": 0, "output": "expected result"}'}]}}
            ]}))
            report = diagnose.diagnose_verdict(result / "verdict.json", root / "trials", 800)
            self.assertIn("step=2", report)
            self.assertIn("原始 rc=0，重放 rc=127", report)
            self.assertIn("expected result", report)
            self.assertIn("missing-tool: not found", report)
            self.assertIn("cd /app && missing-tool", report)

    def test_log_without_verdict_still_produces_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "prepare.log"
            log.write_text("replay failed before verdict\n")
            self.assertEqual(diagnose.find_targets(log.parent), [("log", log)])
            self.assertIn("replay failed before verdict", diagnose.diagnose_log(log))


if __name__ == "__main__":
    unittest.main()
