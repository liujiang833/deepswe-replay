"""CPU affinity list parsing used before replay and for live verification."""

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("deepswe_replay", Path(__file__).resolve().parents[1] / "replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class CpuSetTests(unittest.TestCase):
    def test_lists_and_ranges_have_the_same_cpu_ids(self):
        self.assertEqual(replay.cpuset_ids("0,2-3"), {0, 2, 3})
        self.assertEqual(replay.cpuset_ids("0,2,3"), {0, 2, 3})

    def test_invalid_ranges_fail_before_docker_run(self):
        for value in ("2-0", "0,,2", "0;2", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replay.cpuset_ids(value)


if __name__ == "__main__":
    unittest.main()
