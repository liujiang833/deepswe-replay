"""Selected-step replay must respect step, rather than command, boundaries."""

import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_step_dockers as prepare
import cleanup_step_dockers as cleanup
import replay_selected_steps as selected
import replay as replay_module
from replay import load_trace, select_trace_commands


def trajectory():
    steps = []
    for commands in (("a",), ("b", "c"), ("d",), ("e",), ("f",)):
        steps.append({"tool_calls": [{"arguments": {"command": command}} for command in commands],
                      "observation": {"results": [{"content": '{"returncode": 0}'}
                                                  for _ in commands]}})
    return {"steps": steps}


class SelectedStepReplayTest(unittest.TestCase):
    def test_original_failure_replay_success_is_accepted_and_other_divergence_is_not(self):
        cases = [(-1, 0, False, "relaxed"), (1, 0, False, "relaxed"),
                 (0, 0, False, "strict"), (-1, 124, True, "semantic"),
                 (0, 1, False, "mismatch"), (None, 0, False, "mismatch")]
        for trace_rc, replay_rc, timed_out, expected in cases:
            with self.subTest(trace_rc=trace_rc, replay_rc=replay_rc):
                self.assertEqual(replay_module.rc_match_kind(trace_rc, replay_rc, timed_out), expected)

    def test_harness_timeout_ignores_inner_600_seconds_and_returns_minus_one(self):
        command = "timeout 600 true"

        def expire(argv, **kwargs):
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(argv[5:7], ["/bin/sh", "-c"])
            raise subprocess.TimeoutExpired(argv, 30, output=b"partial output")

        with patch.object(replay_module.subprocess, "run", side_effect=expire):
            rc, stdout, _, outer, timed_out, limit = replay_module.execute_docker_command(
                "container", ["/bin/sh", "-c"], command, 30, "harness")
        self.assertEqual((rc, stdout, outer, timed_out, limit),
                         (-1, b"partial output", True, True, 30))

    def test_adaptive_timeout_still_extends_inner_600_seconds(self):
        def complete(argv, **kwargs):
            self.assertEqual(kwargs["timeout"], 670)
            self.assertEqual(argv[5:9], ["timeout", "-k", "5", "610"])
            return subprocess.CompletedProcess(argv, 0, b"done", b"")

        with patch.object(replay_module.subprocess, "run", side_effect=complete):
            rc, _, _, outer, timed_out, limit = replay_module.execute_docker_command(
                "container", ["/bin/sh", "-c"], "timeout 600 true", 30, "adaptive")
        self.assertEqual((rc, outer, timed_out, limit), (0, False, False, 610))

    def test_command_range_keeps_every_command_in_step(self):
        items = load_trace(trajectory())
        todo, skipped = select_trace_commands(items, 1, 2)
        self.assertEqual([(i, it["cmd"]) for i, it in todo], [(1, "b"), (2, "c")])
        self.assertEqual(skipped, 0)

    def test_prepare_normalizes_one_based_step_and_reuses_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trials" / "sample"
            trial.mkdir(parents=True)
            (trial / "trajectory.json").write_text(json.dumps(trajectory()))
            (trial / "task.json").write_text(json.dumps({"files": [
                {"path": "task.toml", "content": 'docker_image = "example:fixed"\n'}]}))
            input_csv = root / "exp_steps.csv"
            input_csv.write_text("trial,step,cluster\nsample,5,C1\nsample,5,C2\n")
            output_csv = root / "exp_steps_docker.csv"
            argv = ["prepare_step_dockers.py", str(input_csv), "--trials-dir", str(root / "trials"),
                    "--logs-dir", str(root / "logs"),
                    "--step-base", "1", "--buffer-steps", "2"]
            with patch.object(sys, "argv", argv), \
                 patch.object(prepare, "image_id", return_value="sha256:base"), \
                 patch.object(prepare, "prepare_checkpoint", return_value=("sha256:prepared", 1)) as commit, \
                 patch.object(prepare, "docker", return_value=subprocess.CompletedProcess([], 0)):
                self.assertEqual(prepare.main(), 0)
            with output_csv.open(newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["step"] for row in rows], ["4", "4"])
            self.assertEqual([row["checkpoint_step"] for row in rows], ["2", "2"])
            self.assertEqual([row["warmup_steps"] for row in rows], ["2", "2"])
            self.assertEqual([row["checkpoint_rc_relaxed_count"] for row in rows], ["1", "1"])
            self.assertTrue(all(row["timeout_policy"] == "harness" for row in rows))
            self.assertTrue(all(row["image_tag"].startswith("example:fixed-") and
                                row["image_tag"].endswith("-replay-step5") for row in rows))
            self.assertEqual([row["cluster"] for row in rows], ["C1", "C2"])
            self.assertEqual(commit.call_count, 1)

    def test_prepare_dry_run_writes_plan_without_creating_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trials" / "sample"
            trial.mkdir(parents=True)
            (trial / "trajectory.json").write_text(json.dumps(trajectory()))
            (trial / "task.json").write_text(json.dumps({"files": [
                {"path": "task.toml", "content": 'docker_image = "example:fixed"\n'}]}))
            input_csv = root / "exp_steps.csv"
            input_csv.write_text("trial,step\nsample,4\n")
            output_csv = root / "exp_steps_docker.plan.csv"
            argv = ["prepare_step_dockers.py", str(input_csv), "--trials-dir", str(root / "trials"),
                    "--dry-run", "--buffer-steps", "2"]
            with patch.object(sys, "argv", argv), \
                 patch.object(prepare, "image_id", return_value="sha256:base"), \
                 patch.object(prepare, "prepare_checkpoint") as commit, \
                 patch.object(prepare, "docker") as docker_call:
                self.assertEqual(prepare.main(), 0)
            commit.assert_not_called()
            docker_call.assert_not_called()
            with output_csv.open(newline="") as f:
                row = next(csv.DictReader(f))
            self.assertEqual(row["status"], "planned")
            self.assertEqual(row["checkpoint_step"], "2")
            self.assertEqual(row["image_id"], "")
            self.assertTrue(row["image_tag"].endswith("-replay-step4"))

    def test_prepare_retry_keeps_ready_row_and_replays_only_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trials" / "sample"
            trial.mkdir(parents=True)
            (trial / "trajectory.json").write_text(json.dumps(trajectory()))
            (trial / "task.json").write_text(json.dumps({"files": [
                {"path": "task.toml", "content": 'docker_image = "example:fixed"\n'}]}))
            manifest = root / "steps_docker.csv"
            fields = ["trial", "step", "input_step", "step_base", "warmup_steps",
                      "image_tag", "image_id", "status", "error"]
            with manifest.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"trial": "sample", "step": "1", "input_step": "2",
                                 "step_base": "1", "warmup_steps": "1", "image_id": "sha256:old",
                                 "status": "ready"})
                writer.writerow({"trial": "sample", "step": "4", "input_step": "5",
                                 "step_base": "1", "warmup_steps": "2", "status": "error"})
            argv = ["prepare_step_dockers.py", str(manifest), "--trials-dir", str(root / "trials"),
                    "--retry-failed", "--logs-dir", str(root / "logs")]
            with patch.object(sys, "argv", argv), \
                 patch.object(prepare, "image_id", return_value="sha256:base"), \
                 patch.object(prepare, "prepare_checkpoint", return_value=("sha256:new", 0)) as commit:
                self.assertEqual(prepare.main(), 0)
            with manifest.open(newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["image_id"], "sha256:old")
            self.assertEqual(rows[0]["status"], "ready")
            self.assertEqual(rows[1]["step"], "4")
            self.assertEqual(rows[1]["checkpoint_step"], "2")
            self.assertEqual(rows[1]["input_step"], "5")
            self.assertEqual(rows[1]["status"], "ready")
            commit.assert_called_once()

    def test_measure_retry_selects_only_failed_result_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / "results.csv"
            fields = ["trial", "step", "checkpoint_step", "warmup_steps", "image_tag",
                      "image_id", "trace_sha256", "task_sha256", "status", "error"]
            with previous.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for status in ("ok", "error"):
                    writer.writerow({"trial": "sample", "step": "4", "checkpoint_step": "2",
                                     "warmup_steps": "2", "image_tag": "example:fixed-replay-step4",
                                     "image_id": "sha256:prepared", "trace_sha256": "trace",
                                     "task_sha256": "task", "status": status})
            argv = ["replay_selected_steps.py", str(previous), "--retry-failed",
                    "--output-dir", str(root / "retry")]
            with patch.object(sys, "argv", argv), patch.object(selected, "run_one") as run_one:
                run_one.side_effect = lambda row, *_: dict(row, status="ok", error="")
                self.assertEqual(selected.main(), 0)
            run_one.assert_called_once()
            self.assertEqual(run_one.call_args.args[0]["status"], "ready")
            with (root / "retry" / "results.csv").open(newline="") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_initial_checkpoint_gets_its_own_labeled_image(self):
        calls = []

        def fake_docker(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "sha256:prepared\n", "")

        with patch.object(prepare, "docker", side_effect=fake_docker), \
             patch.object(prepare, "image_id", return_value="sha256:prepared"):
            prepared = prepare.prepare_checkpoint(Path("sample"), 0, "example:fixed",
                                                  Path("unused"), "example:fixed-abc-replay-step0",
                                                  "abcdef0123456789abcd")
        self.assertEqual(prepared, ("sha256:prepared", 0))
        self.assertEqual(calls[0][0], "create")
        self.assertEqual(calls[1][0], "commit")
        self.assertIn(f"LABEL {prepare.REPLAY_LABEL}=true", calls[1])
        self.assertEqual(calls[2][0], "rm")

    def test_cleanup_rejects_original_image_without_replay_label(self):
        source = "abcdef0123456789abcd"
        tag = "example:fixed-abcdef012345-replay-step5"
        images = {
            tag: {"Id": "sha256:prepared", "RepoTags": [tag],
                  "Config": {"Labels": {prepare.REPLAY_LABEL: "true",
                                        prepare.SOURCE_LABEL: source}}},
            "example:fixed": {"Id": "sha256:base", "RepoTags": ["example:fixed"],
                              "Config": {"Labels": {}}},
        }
        with patch.object(cleanup, "inspect_image", side_effect=images.__getitem__), \
             patch.object(cleanup, "docker", return_value=subprocess.CompletedProcess([], 0)) as docker_call:
            removed, failed = cleanup.remove_candidates(
                [(tag, "sha256:prepared"), ("example:fixed", "sha256:base")])
        self.assertEqual((removed, failed), (1, 1))
        docker_call.assert_called_once_with("image", "rm", tag)

    def test_cleanup_all_finds_only_marked_tags_and_dangling_images(self):
        source = "abcdef0123456789abcd"
        tag = "example:fixed-abcdef012345-replay-step5"
        images = {
            "sha256:prepared": {"Id": "sha256:prepared", "RepoTags": [tag, "example:keep"],
                                "Config": {"Labels": {prepare.REPLAY_LABEL: "true",
                                                      prepare.SOURCE_LABEL: source}}},
            "sha256:dangling": {"Id": "sha256:dangling", "RepoTags": ["<none>:<none>"],
                                "Config": {"Labels": {prepare.REPLAY_LABEL: "true",
                                                      prepare.SOURCE_LABEL: source}}},
            "sha256:base": {"Id": "sha256:base", "RepoTags": ["example:fixed"],
                            "Config": {"Labels": {}}},
        }
        listing = "sha256:prepared\nsha256:dangling\nsha256:base\n"
        with patch.object(cleanup, "docker", return_value=subprocess.CompletedProcess([], 0, listing, "")), \
             patch.object(cleanup, "inspect_image", side_effect=images.__getitem__):
            candidates = cleanup.all_candidates()
        self.assertEqual(candidates, [(tag, "sha256:prepared"),
                                      ("sha256:dangling", "sha256:dangling")])

    def test_replay_extracts_only_target_step_and_checks_perf_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trials" / "sample"
            trial.mkdir(parents=True)
            raw = json.dumps(trajectory()).encode()
            (trial / "trajectory.json").write_bytes(raw)
            task_raw = b'{}'
            (trial / "task.json").write_bytes(task_raw)
            row = {"trial": "sample", "step": "4", "checkpoint_step": "2",
                   "warmup_steps": "2", "image_tag": "example:fixed-abc-replay-step5",
                   "image_id": "sha256:prepared",
                   "trace_sha256": hashlib.sha256(raw).hexdigest(),
                   "task_sha256": hashlib.sha256(task_raw).hexdigest(),
                   "timeout_policy": "harness", "status": "ready"}

            def fake_run(cmd, **_):
                if cmd[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(cmd, 0, "sha256:prepared\n", "")
                self.assertEqual(cmd[0], "bash")
                self.assertEqual(cmd[cmd.index("--start-step") + 1], "2")
                self.assertEqual(cmd[cmd.index("--stop-before-step") + 1], "5")
                self.assertEqual(cmd[cmd.index("--timeout-policy") + 1], "harness")
                output = Path(cmd[cmd.index("-o") + 1]) / "sample"
                td = output / "topdown"
                td.mkdir(parents=True)
                (td / "run_status.json").write_text(json.dumps(
                    {"replay_rc": 0, "perf_rc": 0, "parse_rc": 0}))
                (output / "verdict.json").write_text(json.dumps(
                    {"start_step": 2, "stop_before_step": 5,
                     "timeout_policy": "harness", "n_replayed": 3,
                     "rc_match_semantic": "2/3", "rc_match_relaxed": "3/3",
                     "rc_relaxed_count": 1}))
                (output / "commands.jsonl").write_text("\n".join(
                    json.dumps({"step": n}) for n in (2, 3, 4)))
                (td / "topdown_steps_cleaned.json").write_text(json.dumps({
                    "interval_ms": 10,
                    "steps": [{"step": 4, "n_cmds": 1, "t_start_perf": 0.05,
                               "t_end_perf": 0.20, "counts": {"cpu_cycles": 123},
                               "topdown": {"Retiring": 0.1, "BadSpec": 0.2,
                                           "FrontendBound": 0.3, "BackendBound": 0.4}}]}))
                (td / "perf.json").write_text("\n".join(
                    json.dumps({"interval": n}) for n in (0.01, 0.05, 0.10, 0.20, 0.21)))
                return subprocess.CompletedProcess(cmd, 0)

            with patch.object(selected.subprocess, "run", side_effect=fake_run):
                result = selected.run_one(row, 1, 1, root / "trials", root / "runs", "")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["target_cycles"], "123")
            self.assertEqual(result["Retiring"], "0.1")
            self.assertEqual(result["rc_relaxed_count"], "1")


if __name__ == "__main__":
    unittest.main()
