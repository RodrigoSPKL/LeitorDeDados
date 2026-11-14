"""Checks for capacity arithmetic and invalid scenario boundaries."""

from dataclasses import replace
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "estimate_capacity.py"
SPEC = importlib.util.spec_from_file_location("estimate_capacity", SCRIPT)
capacity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity
SPEC.loader.exec_module(capacity)


class CapacityTests(unittest.TestCase):
    def test_human_report_uses_brazilian_number_format(self):
        result = capacity.estimate_capacity(capacity.Assumptions(
            rows=20_000_000_000, avg_row_bytes=1000, compression_ratio=3,
            copies=2, overhead_pct=25, free_space_pct=30, backup_copies=1,
            rows_per_day=100_000, target_load_hours=24,
        ))
        report = capacity.human_report(result)
        self.assertIn("Registros: 20.000.000.000", report)
        self.assertIn("Bytes lógicos/registro: 1.000", report)
        self.assertIn("20,000 TB / 18,190 TiB", report)
        self.assertIn("23,810 TB", report)
        self.assertIn("100.000 registros/dia; 200.000,00 dias", report)
        self.assertIn("231.481,48 registros/s", report)
        self.assertEqual(capacity.format_pt_br(3.5), "3,5")
        self.assertEqual(result["storage"]["logical_data"]["TB"], 20)

    def test_reference_scenario_and_units(self):
        scenario = capacity.Assumptions(
            rows=20_000_000_000, avg_row_bytes=1000, compression_ratio=4,
            copies=2, overhead_pct=20, free_space_pct=25, backup_copies=1,
        )
        result = capacity.estimate_capacity(scenario)
        self.assertEqual(result["storage"]["logical_data"]["TB"], 20)
        self.assertEqual(result["storage"]["compressed_data"]["TB"], 5)
        self.assertEqual(result["storage"]["cluster_occupied"]["TB"], 12)
        self.assertEqual(result["storage"]["cluster_provisioned"]["TB"], 16)
        self.assertEqual(result["storage"]["backups_separate"]["TB"], 6)
        self.assertAlmostEqual(
            result["storage"]["cluster_provisioned"]["TiB"], 16e12 / 2**40,
        )

    def test_cluster_replication_does_not_multiply_backups(self):
        first = capacity.estimate_capacity(capacity.Assumptions(copies=1))
        second = capacity.estimate_capacity(capacity.Assumptions(copies=3))
        self.assertEqual(first["storage"]["backups_separate"],
                         second["storage"]["backups_separate"])
        self.assertTrue(math.isclose(second["storage"]["cluster_provisioned"]["bytes"],
                                     first["storage"]["cluster_provisioned"]["bytes"] * 3,
                                     rel_tol=1e-12))

    def test_growth_and_initial_load_are_independent(self):
        scenario = capacity.Assumptions(
            rows=3_600_000, rows_per_day=100_000, target_load_hours=2,
        )
        result = capacity.estimate_capacity(scenario)
        self.assertEqual(result["growth_days_from_empty"], 36)
        self.assertEqual(result["initial_load_rows_per_second"], 500)
        changed = capacity.estimate_capacity(replace(scenario, rows_per_day=200_000))
        self.assertEqual(changed["initial_load_rows_per_second"], 500)
        self.assertEqual(changed["growth_days_from_empty"], 18)

    def test_zero_overhead_no_compression_no_backup(self):
        result = capacity.estimate_capacity(capacity.Assumptions(
            rows=10, avg_row_bytes=10, compression_ratio=1, copies=1,
            overhead_pct=0, free_space_pct=0, backup_copies=0,
        ))
        self.assertEqual(result["storage"]["cluster_provisioned"]["bytes"], 100)
        self.assertEqual(result["storage"]["backups_separate"]["bytes"], 0)
        self.assertIsNone(result["growth_days_from_empty"])

    def test_invalid_boundaries(self):
        for field, value in [
            ("rows", 0), ("rows", -1), ("rows", 1.5), ("rows", True),
            ("avg_row_bytes", 0), ("compression_ratio", 0.5), ("copies", 0),
            ("copies", 1.5), ("overhead_pct", -1), ("free_space_pct", -1),
            ("free_space_pct", 100), ("backup_copies", -1),
            ("backup_copies", 0.5), ("rows_per_day", 0), ("target_load_hours", -1),
        ]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    capacity.estimate_capacity(replace(capacity.Assumptions(), **{field: value}))

    def test_non_finite_inputs_rejected(self):
        for field in ("avg_row_bytes", "compression_ratio", "overhead_pct",
                      "free_space_pct", "rows_per_day", "target_load_hours"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        capacity.estimate_capacity(replace(capacity.Assumptions(), **{field: value}))

    def test_overflow_rejected(self):
        with self.assertRaises(ValueError):
            capacity.estimate_capacity(capacity.Assumptions(avg_row_bytes=1e308))

    def test_cli_json_and_invalid_input(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            capture_output=True, text=True, check=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["assumptions"]["rows"], 20_000_000_000)
        self.assertGreater(result["storage"]["cluster_provisioned"]["TB"], 0)
        invalid = subprocess.run(
            [sys.executable, str(SCRIPT), "--compression-ratio", "nan"],
            capture_output=True, text=True,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("finito", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
