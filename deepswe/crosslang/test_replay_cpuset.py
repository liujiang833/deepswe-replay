"""CPU affinity list parsing used before replay and for live verification."""

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("deepswe_replay", Path(__file__).resolve().parents[1] / "replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)
crosslang_spec = importlib.util.spec_from_file_location("crosslang_replay", Path(__file__).resolve().parent / "replay.py")
crosslang_replay = importlib.util.module_from_spec(crosslang_spec)
crosslang_spec.loader.exec_module(crosslang_replay)


class CpuSetTests(unittest.TestCase):
    def test_lists_and_ranges_have_the_same_cpu_ids(self):
        self.assertEqual(replay.cpuset_ids("0,2-3"), {0, 2, 3})
        self.assertEqual(replay.cpuset_ids("0,2,3"), {0, 2, 3})

    def test_invalid_ranges_fail_before_docker_run(self):
        for value in ("2-0", "0,,2", "0;2", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replay.cpuset_ids(value)

    def test_cpuset_overrides_task_cpu_quota(self):
        for module in (replay, crosslang_replay):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.docker_cpu_args("2", ""), ["--cpus=2"])
                self.assertEqual(module.docker_cpu_args("2", "3"), ["--cpuset-cpus=3"])
                self.assertEqual(module.docker_cpu_args("2", "0,2"), ["--cpuset-cpus=0,2"])


if __name__ == "__main__":
    unittest.main()
