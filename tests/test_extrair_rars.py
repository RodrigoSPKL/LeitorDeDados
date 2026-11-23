import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import extrair_rars as rar


class ExtractRarsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.archive = self.root / "file with [spaces].rar"
        self.archive.write_bytes(b"original archive")

    def inspect_stub(self, paths=None, entries=None, properties=None):
        data = {"lsarFormatName": "RAR 5", "lsarContents": entries or [{"XADFileName": "data.csv", "XADFileSize": 10}],
                "lsarProperties": {"XADVolumes": [str(path) for path in paths or [self.archive]], **(properties or {})}}

        def run(command, **kwargs):
            kwargs["stdout"].write(json.dumps(data).encode())
            return SimpleNamespace(returncode=0)
        return run

    def extract_stub(self, command, **kwargs):
        staging = Path(command[command.index("-o") + 1])
        (staging / "data.csv").write_text("code\nTEST\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def run_folder(self, inspect=None, extractor=None, depth=12, backend="unar"):
        with patch.object(rar.shutil, "which", return_value="tool"), patch.object(
            rar, "_inspect", side_effect=inspect or (lambda archive: ([archive], 10))
        ), patch.object(rar.subprocess, "run", side_effect=extractor or self.extract_stub), redirect_stdout(io.StringIO()):
            return rar.extract_folder(self.root, depth, backend)

    def unrar_stub(self, command, **kwargs):
        self.assertEqual(command[:5], ["unrar", "x", "-cfg-", "-o-", "-p-"])
        self.assertNotIn("RAR", kwargs["env"])
        self.assertEqual(command[-2], str(self.archive))
        staging = Path(command[-1])
        self.assertNotEqual(staging, self.root)
        (staging / "data.csv").write_bytes(b"complete output")
        kwargs["stdout"].write(b"All OK\n")
        return SimpleNamespace(returncode=0)

    def test_unrar_success_performs_full_extraction_and_then_backs_up_original(self):
        report = self.run_folder(extractor=self.unrar_stub, backend="unrar")
        self.assertEqual(report["backend"], "unrar")
        self.assertEqual(report["extracted_sets"], 1)
        self.assertFalse(report["failures"])
        self.assertFalse(self.archive.exists())
        self.assertEqual(next((self.root / "backup").rglob("*.rar")).read_bytes(), b"original archive")
        self.assertEqual((self.root / "file with [spaces]_extraido" / "data.csv").read_bytes(), b"complete output")
        self.assertIn("All OK", next((self.root / "backup").rglob("*.unrar.log")).read_text())

    def test_restore_output_override_keeps_backup_archives_in_place(self):
        saved = self.root / "backup" / "old-run"
        saved.mkdir(parents=True)
        original = saved / self.archive.name
        self.archive.rename(original)
        output = self.root / "fresh" / "archive_extraido"

        def extract(command, **kwargs):
            self.assertEqual(command[-2], str(original))
            (Path(command[-1]) / "data.csv").write_bytes(b"fresh")
            return SimpleNamespace(returncode=0)

        with patch.object(rar.subprocess, "run", side_effect=extract), redirect_stdout(io.StringIO()):
            result = rar._extract_set(original, [original], 10, self.root, self.root / "backup" / "new-run", "unrar",
                                      output_override=output, keep_sources=True)
        self.assertEqual(result, output)
        self.assertEqual(original.read_bytes(), b"original archive")
        self.assertEqual((output / "data.csv").read_bytes(), b"fresh")

    def test_unrar_failure_preserves_original_and_reports_backend_error(self):
        def fail(command, **kwargs):
            self.unrar_stub(command, **kwargs)
            kwargs["stdout"].write(b"CRC failed\n")
            return SimpleNamespace(returncode=3)
        report = self.run_folder(extractor=fail, backend="unrar")
        failure = report["failures"][0]
        self.assertEqual((failure["tool"], failure["tool_exit_code"]), ("unrar", 3))
        self.assertIn("CRC failed", failure["diagnostic"])
        self.assertEqual(self.archive.read_bytes(), b"original archive")
        self.assertFalse(list((self.root / "backup").rglob("*.rar")))

    def test_duplicate_member_names_are_rejected_before_any_extraction(self):
        entries = [{"XADFileName": name, "XADFileSize": 1} for name in ("data.csv", "DATA.csv")]
        with patch.object(rar.subprocess, "run", side_effect=self.inspect_stub(entries=entries)), self.assertRaisesRegex(rar.ExtractionError, "repetidos"):
            rar._inspect(self.archive)

    def test_missing_unrar_fails_before_creating_backup_or_lock(self):
        with patch.object(rar.shutil, "which", side_effect=lambda name: None if name == "unrar" else "lsar"):
            with self.assertRaisesRegex(rar.ExtractionError, "Instale"):
                rar.extract_folder(self.root, backend="unrar")
        self.assertTrue(self.archive.exists())
        self.assertFalse((self.root / "backup").exists())
        self.assertFalse((self.root / ".rar-extraction-lock").exists())

    def test_inspection_identifies_volumes_and_bounds_output_paths(self):
        second = self.root / "second.rar"
        second.write_bytes(b"second")
        with patch.object(rar.subprocess, "run", side_effect=self.inspect_stub([self.archive, second])):
            self.assertEqual(rar._inspect(self.archive), ([self.archive, second], 10))
        for name in ("../escape", "/absolute", "C:\\escape", "folder/../../escape"):
            with self.subTest(name=name), patch.object(rar.subprocess, "run", side_effect=self.inspect_stub(entries=[{"XADFileName": name, "XADFileSize": 1}])):
                with self.assertRaises(rar.ExtractionError):
                    rar._inspect(self.archive)

    def test_inspection_rejects_missing_volumes_failed_scans_and_links(self):
        cases = [self.inspect_stub([self.archive, self.root / "missing.rar"]),
                 self.inspect_stub(properties={"XADVolumeScanningFailed": True}),
                 self.inspect_stub(entries=[{"XADFileName": "link", "XADIsLink": True, "XADFileSize": 0}]),
                 self.inspect_stub(entries=[{"XADFileName": "pipe", "XADIsFIFO": True, "XADFileSize": 0}])]
        for stub in cases:
            with patch.object(rar.subprocess, "run", side_effect=stub), self.assertRaises(rar.ExtractionError):
                rar._inspect(self.archive)

    def test_success_preserves_data_and_moves_original_only_after_extracting(self):
        unrelated = self.root / "existing.csv"
        unrelated.write_bytes(b"keep")
        report = self.run_folder()
        self.assertFalse(report["failures"])
        self.assertEqual((report["extracted_sets"], report["backed_up_volumes"], report["remaining_rars"]), (1, 1, 0))
        self.assertEqual(next((self.root / "backup").rglob("*.rar")).read_bytes(), b"original archive")
        self.assertFalse(self.archive.exists())
        self.assertTrue((self.root / "file with [spaces]_extraido" / "data.csv").is_file())
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertFalse((self.root / ".rar-extraction-lock").exists())

    def test_failed_extraction_keeps_archive_and_partial_output_outside_backup(self):
        def failed(command, **kwargs):
            self.extract_stub(command, **kwargs)
            kwargs["stdout"].write(b"Extracting data.csv\nChecksum error: data.csv\n")
            return SimpleNamespace(returncode=1)
        report = self.run_folder(extractor=failed)
        self.assertEqual(len(report["failures"]), 1)
        failure = report["failures"][0]
        self.assertEqual((failure["tool"], failure["tool_exit_code"]), ("unar", 1))
        self.assertIn("Checksum error", failure["diagnostic"])
        self.assertIn("Checksum error", Path(failure["log"]).read_text())
        self.assertTrue(Path(failure["partial_output"]).is_dir())
        self.assertEqual(json.loads((self.root / "backup" / "last-extraction-report.json").read_text(encoding="utf-8")), report)
        self.assertEqual(self.archive.read_bytes(), b"original archive")
        self.assertFalse(list((self.root / "backup").rglob("*.rar")))
        self.assertTrue(list(self.root.glob(".rar-pending-*")))

    def test_lsar_failure_preserves_backend_message_and_exit_status(self):
        def failed(command, **kwargs):
            kwargs["stderr"].write(b"Cannot open next volume: missing.part0002.rar\n")
            return SimpleNamespace(returncode=2)
        with patch.object(rar.subprocess, "run", side_effect=failed), self.assertRaises(rar.ExtractionError) as caught:
            rar._inspect(self.archive)
        self.assertIn("Cannot open next volume", str(caught.exception))
        self.assertEqual(caught.exception.details["tool_exit_code"], 2)
        self.assertEqual(self.archive.read_bytes(), b"original archive")

    def test_diagnostic_tail_is_bounded_and_does_not_emit_terminal_escape_sequences(self):
        stream = io.BytesIO(b"old output" * 2000 + b"\nLAST: \x1b[31mCRC failure\x00\n")
        diagnostic = rar._diagnostic_tail(stream)
        self.assertIn("LAST:", diagnostic)
        self.assertNotIn("\x1b", diagnostic)
        self.assertNotIn("\0", diagnostic)
        self.assertIn("\\x1b", diagnostic)
        self.assertLess(len(diagnostic), 4200)

    def test_wrong_format_reports_detected_format_instead_of_generic_rar_error(self):
        def not_rar(command, **kwargs):
            kwargs["stdout"].write(json.dumps({"lsarFormatName": "7-Zip", "lsarContents": []}).encode())
            return SimpleNamespace(returncode=0)
        with patch.object(rar.subprocess, "run", side_effect=not_rar), self.assertRaisesRegex(rar.ExtractionError, "7-Zip"):
            rar._inspect(self.archive)

    def test_multipart_is_processed_once_and_all_volumes_remain_together(self):
        self.archive.unlink()
        parts = [self.root / f"set.part{i:04}.rar" for i in range(1, 4)]
        for part in parts:
            part.write_bytes(b"part")
        report = self.run_folder(inspect=lambda archive: (parts, 10))
        self.assertEqual((report["extracted_sets"], report["backed_up_volumes"]), (1, 3))
        saved = list((self.root / "backup").rglob("*.rar"))
        self.assertEqual({path.name for path in saved}, {part.name for part in parts})
        self.assertEqual(len({path.parent for path in saved}), 1)

    def test_existing_destination_does_not_overwrite_or_move_archive(self):
        output = self.root / "file with [spaces]_extraido"
        output.mkdir()
        (output / "keep").write_bytes(b"keep")
        report = self.run_folder()
        self.assertTrue(report["failures"])
        self.assertTrue(self.archive.exists())
        self.assertEqual((output / "keep").read_bytes(), b"keep")

    def test_nested_directories_and_nested_rars_are_processed_except_backup(self):
        ignored = self.root / "other" / "backup"
        ignored.mkdir(parents=True)
        (ignored / "ignored.rar").write_bytes(b"keep")

        def nested(command, **kwargs):
            staging = Path(command[command.index("-o") + 1])
            if command[-1] == str(self.archive):
                (staging / "inner.rar").write_bytes(b"inner")
            else:
                (staging / "done.txt").write_bytes(b"done")
            return SimpleNamespace(returncode=0)
        report = self.run_folder(extractor=nested)
        self.assertEqual(report["extracted_sets"], 2)
        self.assertEqual((ignored / "ignored.rar").read_bytes(), b"keep")
        self.assertEqual(len(list((self.root / "backup").rglob("*.rar"))), 2)

    def test_failed_backup_move_restores_previous_volumes(self):
        second = self.root / "second.rar"
        second.write_bytes(b"second")
        original_rename = Path.rename

        def fail_second(source, target):
            if source == second:
                raise OSError("simulated move failure")
            return original_rename(source, target)
        with patch.object(Path, "rename", fail_second):
            report = self.run_folder(inspect=lambda archive: ([self.archive, second], 10))
        self.assertTrue(report["failures"])
        self.assertEqual(self.archive.read_bytes(), b"original archive")
        self.assertEqual(second.read_bytes(), b"second")
        self.assertFalse(list((self.root / "backup").rglob("*.rar")))


if __name__ == "__main__":
    unittest.main()
