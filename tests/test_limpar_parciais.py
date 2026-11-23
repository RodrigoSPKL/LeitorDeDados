import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import limpar_parciais as clean


class CleanupPartialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.partial = self.root / ".rar-pending-ab12_cd3"
        self.partial.mkdir()
        (self.partial / "data.csv").write_bytes(b"partial")
        self.complete = self.root / "archive_extraido"
        self.complete.mkdir()
        self.original_file = self.complete / "original (2).csv"
        self.original_file.write_bytes(b"keep original data")
        self.backup = self.root / "backup"
        self.backup.mkdir()
        (self.backup / "original.rar").write_bytes(b"original compressed")

    def assert_originals_kept(self):
        self.assertEqual(self.original_file.read_bytes(), b"keep original data")
        self.assertEqual((self.backup / "original.rar").read_bytes(), b"original compressed")

    def test_default_is_preview_without_moving_or_removing_anything(self):
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        result = clean.cleanup(self.root)
        self.assertEqual(result["mode"], "preview")
        self.assertEqual(result["total_bytes"], 7)
        self.assertEqual([item["path"] for item in result["candidates"]], [self.partial.name])
        self.assertFalse(result["processed"])
        self.assertEqual(before, sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*")))
        self.assert_originals_kept()

    def test_quarantine_preserves_bytes_and_records_exact_move(self):
        result = clean.cleanup(self.root, "quarantine")
        self.assertFalse(result["errors"])
        self.assertFalse(self.partial.exists())
        destination = Path(result["processed"][0]["destination"])
        self.assertTrue(destination.is_relative_to(self.backup))
        self.assertEqual((destination / "data.csv").read_bytes(), b"partial")
        self.assertEqual(json.loads(Path(result["manifest"]).read_text(encoding="utf-8")), result)
        self.assert_originals_kept()
        self.assertFalse(clean.cleanup(self.root)["candidates"])

    def test_delete_removes_only_generated_partials_and_preserves_complete_outputs(self):
        result = clean.cleanup(self.root, "delete")
        self.assertFalse(result["errors"])
        self.assertFalse(self.partial.exists())
        self.assertEqual(result["processed"], [{"source": self.partial.name, "deleted": True}])
        self.assert_originals_kept()

    def test_nested_partial_is_selected_without_selecting_completed_parent(self):
        nested = self.complete / ".rar-pending-12345678"
        nested.mkdir()
        (nested / "partial.csv").write_bytes(b"nested")
        result = clean.cleanup(self.root, "delete")
        self.assertEqual(len(result["processed"]), 2)
        self.assertTrue(self.complete.is_dir())
        self.assertFalse(nested.exists())
        self.assert_originals_kept()

    def test_normal_duplicate_names_and_unrecognized_directory_names_are_not_selected(self):
        for name in ("file (2)", "second_extraido", ".rar-pending-manual", "normal"):
            (self.root / name).mkdir()
        result = clean.cleanup(self.root, "delete")
        self.assertEqual(len(result["processed"]), 1)
        for name in ("file (2)", "second_extraido", ".rar-pending-manual", "normal"):
            self.assertTrue((self.root / name).is_dir())
        self.assert_originals_kept()

    def test_active_root_extraction_lock_prevents_cleanup(self):
        (self.root / ".rar-extraction-lock").mkdir()
        with self.assertRaisesRegex(clean.CleanupError, "reserva"):
            clean.cleanup(self.root, "delete")
        self.assertTrue(self.partial.is_dir())
        self.assertFalse(list(self.backup.glob("limpeza-*")))

    def test_active_child_extraction_lock_prevents_all_mutations(self):
        child = self.root / "other"
        child.mkdir()
        (child / ".rar-extraction-lock").mkdir()
        with self.assertRaisesRegex(clean.CleanupError, "reserva"):
            clean.cleanup(self.root, "delete")
        self.assertTrue(self.partial.is_dir())
        self.assertFalse(list(self.backup.glob("limpeza-*")))

    def test_lock_inside_partial_prevents_deleting_an_active_extraction(self):
        (self.partial / ".rar-extraction-lock").mkdir()
        with self.assertRaisesRegex(clean.CleanupError, "reserva"):
            clean.cleanup(self.root, "delete")
        self.assertTrue(self.partial.is_dir())
        self.assertFalse(list(self.backup.glob("limpeza-*")))

    def test_backup_and_outside_paths_are_never_deletion_targets(self):
        saved = self.backup / ".rar-pending-12345678"
        saved.mkdir()
        with self.assertRaises(clean.CleanupError):
            clean._checked(self.root, saved)
        with self.assertRaises(clean.CleanupError):
            clean._checked(self.root, self.root)
        with self.assertRaises(clean.CleanupError):
            clean._checked(self.root, self.root.parent / ".rar-pending-12345678")
        with self.assertRaises(clean.CleanupError):
            clean.cleanup(self.backup, "delete")
        clean.cleanup(self.root, "delete")
        self.assertTrue(saved.is_dir())
        self.assert_originals_kept()

    def test_reparse_points_are_skipped_without_traversal_or_removal(self):
        original_link_check = clean._link
        with patch.object(clean, "_link", side_effect=lambda path: path == self.partial or original_link_check(path)):
            result = clean.cleanup(self.root, "delete")
        self.assertFalse(result["candidates"])
        self.assertTrue(self.partial.is_dir())
        self.assert_originals_kept()

    def test_changed_directory_identity_is_refused_before_delete(self):
        plan = clean._plan(self.root)
        plan[0]["inode"] += 1
        with patch.object(clean, "_plan", return_value=plan), patch.object(clean.shutil, "rmtree") as delete:
            result = clean.cleanup(self.root, "delete")
        delete.assert_not_called()
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(self.partial.is_dir())
        self.assert_originals_kept()


if __name__ == "__main__":
    unittest.main()
