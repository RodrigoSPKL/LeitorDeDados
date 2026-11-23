from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import refazer_extracao as resetter


class ResetExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.backup = self.root / "backup"
        self.session = self.backup / "20260101-120000-1234abcd"
        self.session.mkdir(parents=True)
        self.archive = self.session / "clients.rar"
        self.archive.write_bytes(b"original compressed archive")
        self.old = self.root / "old-output"
        self.old.mkdir()
        (self.old / "duplicate.csv").write_bytes(b"old")
        self.loose = self.root / "loose.txt"
        self.loose.write_bytes(b"loose")

    def fake_extract(self, archive, volumes, expanded, root, backup_run, backend, **kwargs):
        self.assertEqual(backend, "unrar")
        output = kwargs.get("output_override", archive.parent / (archive.stem + "_extraido"))
        output.mkdir(parents=True)
        (output / "data (2).csv").write_bytes(b"original member with this name")
        if kwargs.get("keep_sources"):
            self.assertTrue(archive.is_relative_to(self.backup))
        else:
            for volume in volumes:
                destination = backup_run / volume.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                volume.rename(destination)
        return output

    def run_reset(self, apply=True, test=None, extract=None, inspect=None):
        with patch.object(resetter.shutil, "which", return_value="tool"), patch.object(
            resetter.rar, "_inspect", side_effect=inspect or (lambda archive: ([archive], 10))
        ), patch.object(resetter, "_test", side_effect=test), patch.object(
            resetter.rar, "_extract_set", side_effect=extract or self.fake_extract
        ), redirect_stdout(io.StringIO()):
            return resetter.reset(self.root, apply)

    def assert_backup_kept(self):
        self.assertEqual(self.archive.read_bytes(), b"original compressed archive")
        self.assertFalse((self.root / ".rar-extraction-lock").exists())

    def test_reset_tests_backup_before_deleting_and_rebuilds_fresh_outputs(self):
        def test(archive, log):
            self.assertTrue(self.old.is_dir())
            self.assertTrue(self.loose.is_file())
        result = self.run_reset(test=test)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(self.old.exists())
        self.assertFalse(self.loose.exists())
        self.assertEqual((self.root / "clients_extraido" / "data (2).csv").read_bytes(), b"original member with this name")
        self.assertEqual({path.name for path in self.root.iterdir()}, {"backup", "clients_extraido"})
        self.assert_backup_kept()

    def test_preview_does_not_delete_or_extract(self):
        result = self.run_reset(apply=False)
        self.assertEqual(result["status"], "preview")
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())
        self.assertFalse((self.root / "clients_extraido").exists())
        self.assert_backup_kept()

    def test_failed_backup_test_never_deletes_existing_data(self):
        with self.assertRaises(resetter.rar.ExtractionError):
            self.run_reset(test=lambda *args: (_ for _ in ()).throw(resetter.rar.ExtractionError("CRC failure")))
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())
        self.assert_backup_kept()

    def test_integrity_command_tests_entire_archive_and_captures_failure(self):
        def fail(command, **kwargs):
            self.assertEqual(command, ["unrar", "t", "-cfg-", "-p-", str(self.archive)])
            self.assertNotIn("RAR", kwargs["env"])
            kwargs["stdout"].write(b"CRC error in another member\n")
            return SimpleNamespace(returncode=3)
        with patch.object(resetter.subprocess, "run", side_effect=fail):
            with self.assertRaisesRegex(resetter.rar.ExtractionError, "CRC error"):
                resetter._test(self.archive, self.backup / "test.log")
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())
        self.assert_backup_kept()

    def test_logs_only_backup_is_rejected_without_removing_anything(self):
        self.archive.unlink()
        (self.session / "old.log").write_text("log")
        with self.assertRaisesRegex(resetter.rar.ExtractionError, "Backup não contém"):
            self.run_reset()
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())

    def test_identical_archives_across_backup_runs_are_extracted_once(self):
        second = self.backup / "20260102-120000-1234abcd"
        second.mkdir()
        (second / "clients.rar").write_bytes(self.archive.read_bytes())
        result = self.run_reset()
        self.assertEqual(result["extracted_sets"], 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(list((self.root / "clients_extraido").iterdir())), 1)
        self.assert_backup_kept()

    def test_nested_archive_backup_is_not_restored_a_second_time(self):
        nested = self.session / "clients_extraido"
        nested.mkdir()
        (nested / "inner.rar").write_bytes(b"nested")
        result = self.run_reset(apply=False)
        self.assertEqual(len(result["archives"]), 1)
        self.assertIn("RAR pai", result["skipped"][0]["reason"])
        self.assertTrue(self.old.exists())

    def test_conflicting_versions_of_same_output_abort_before_deleting(self):
        second = self.backup / "20260102-120000-1234abcd"
        second.mkdir()
        (second / "clients.rar").write_bytes(b"different original version")
        with self.assertRaisesRegex(resetter.rar.ExtractionError, "mesma pasta"):
            self.run_reset()
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())
        self.assert_backup_kept()

    def test_reset_can_run_twice_without_accumulating_old_outputs(self):
        self.run_reset()
        result = self.run_reset()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(list(self.root.glob("*_extraido"))), 1)
        self.assertEqual(len(list((self.root / "clients_extraido").iterdir())), 1)
        self.assert_backup_kept()

    def test_nested_archive_copies_are_processed_once(self):
        def extract(*args, **kwargs):
            output = self.fake_extract(*args, **kwargs)
            if kwargs.get("keep_sources"):
                (output / "inner1.rar").write_bytes(b"same inner archive")
                (output / "inner2.rar").write_bytes(b"same inner archive")
            return output
        result = self.run_reset(extract=extract)
        self.assertEqual(result["extracted_sets"], 2)
        self.assertFalse(list((self.root / "clients_extraido").glob("*.rar")))
        self.assertEqual(len(list((self.root / "clients_extraido").glob("*_extraido"))), 1)
        self.assert_backup_kept()

    def test_extraction_failure_after_clear_preserves_backup_and_writes_failed_report(self):
        def failed(*args, **kwargs):
            raise resetter.rar.ExtractionError("disk write failure")
        with self.assertRaisesRegex(resetter.rar.ExtractionError, "disk write failure"):
            self.run_reset(extract=failed)
        self.assertFalse(self.old.exists())
        reports = list(self.backup.glob("reset-extracao-*/report.json"))
        self.assertIn('"status": "failed"', reports[0].read_text())
        self.assert_backup_kept()

    def test_backup_changed_during_integrity_test_prevents_removal(self):
        def changed(archive, log):
            archive.write_bytes(b"changed original during test")
        with self.assertRaisesRegex(resetter.rar.ExtractionError, "mudou"):
            self.run_reset(test=changed)
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())

    def test_nested_mount_is_refused_before_removing_data(self):
        original = Path.is_mount
        with patch.object(Path, "is_mount", lambda path: path == self.old or original(path)):
            with self.assertRaisesRegex(resetter.rar.ExtractionError, "disco montado"):
                self.run_reset()
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())
        self.assert_backup_kept()

    def test_backup_folder_itself_and_filesystem_root_are_never_reset_targets(self):
        with self.assertRaises(resetter.rar.ExtractionError):
            resetter.reset(self.backup, True)
        with self.assertRaises(resetter.rar.ExtractionError):
            resetter.reset(Path(self.root.anchor), True)
        self.assertTrue(self.old.exists())
        self.assertTrue(self.loose.exists())
        self.assert_backup_kept()
