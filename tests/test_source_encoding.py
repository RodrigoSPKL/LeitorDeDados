from contextlib import closing
import codecs
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from leitor_dados import consolidate as module
from leitor_dados.consolidate import ConsolidationError, consolidate
from leitor_dados.source_encoding import detect


class SourceEncodingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "LoteEncodings"
        self.root.mkdir()
        self.final = self.root / "LoteEncodings.sqlite"
        self.pending = self.root / ".LoteEncodings.importando.sqlite"

    def write(self, name, value, encoding):
        path = self.root / name
        path.write_text(value, encoding=encoding, newline="")
        return path

    def records(self):
        with closing(sqlite3.connect(self.final)) as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT dados FROM registros")]

    def test_mixed_utf8_cp1252_utf16_and_utf32(self):
        for index, encoding in enumerate(("utf-8", "utf-8-sig", "cp1252", "utf-16", "utf-32")):
            self.write(f"arquivo{index}.csv", "codigo;nome\n001;ação\n", encoding)
        events = []
        result = consolidate(self.root, encoding="auto", remove_sources=True, progress=events.append)
        self.assertEqual(result["registros"], 1)
        self.assertEqual(result["linhas_origem"], 5)
        self.assertEqual(self.records(), [{"codigo": "001", "nome": "ação"}])
        self.assertEqual({event["encoding"] for event in events if event["stage"] == "encoding_selected"},
                         {"utf-8-sig", "cp1252", "utf-16", "utf-32"})

    def test_cp1252_punctuation_is_preserved(self):
        self.write("dados.txt", "“ação” — preço €\n", "cp1252")
        consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertEqual(self.records(), [{"texto": "“ação” — preço €"}])

    def test_utf8_validation_covers_whole_file_not_sample(self):
        source = self.root / "a.txt"
        source.write_bytes(b"a" * (1024 * 1024 + 3) + b"\xe7\n")
        self.assertEqual(detect(source), "cp1252")

    def test_incremental_utf8_accepts_character_split_across_chunks(self):
        source = self.root / "a.txt"
        source.write_bytes(b"a" * (1024 * 1024 - 1) + "ç".encode())
        self.assertEqual(detect(source), "utf-8-sig")

    def test_utf16_big_endian_and_utf32_big_endian_boms(self):
        for marker, encoding, expected in ((codecs.BOM_UTF16_BE, "utf-16-be", "utf-16"),
                                           (codecs.BOM_UTF32_BE, "utf-32-be", "utf-32")):
            path = self.root / "a.txt"
            path.write_bytes(marker + "ação".encode(encoding))
            self.assertEqual(detect(path), expected)

    def test_incomplete_multibyte_sequence_with_bom_fails(self):
        source = self.root / "a.txt"
        source.write_bytes(codecs.BOM_UTF8 + b"texto\xc3")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertTrue(source.exists())

    def test_unknown_bytes_are_not_replaced_or_imported(self):
        source = self.root / "a.txt"
        source.write_bytes(b"registro\x81\n")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertTrue(source.exists())
        self.assertFalse(self.final.exists())
        with closing(sqlite3.connect(self.pending)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM registros").fetchone()[0], 0)

    def test_binary_nul_or_utf16_without_bom_requires_explicit_encoding(self):
        source = self.root / "a.txt"
        source.write_bytes("ação".encode("utf-16-le"))
        with self.assertRaisesRegex(ConsolidationError, "NUL"):
            consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertTrue(source.exists())

    def test_failed_utf8_without_committed_rows_can_upgrade(self):
        source = self.write("a.csv", "codigo;nome\n001;ação\n", "cp1252")
        with self.assertRaisesRegex(ConsolidationError, "--encoding auto"):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())
        result = consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertEqual(result["registros"], 1)
        self.assertEqual(self.records(), [{"codigo": "001", "nome": "ação"}])

    def test_completed_utf8_sources_are_not_reimported_on_simple_upgrade(self):
        self.write("a.txt", "registro utf8: ação", "utf-8")
        self.write("b.txt", "registro cp1252: ação", "cp1252")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root)
        events = []
        consolidate(self.root, encoding="auto", remove_sources=True, progress=events.append)
        self.assertTrue(any(event["stage"] == "source_already_imported" and event["arquivo"] == "a.txt" for event in events))
        self.assertEqual(len(self.records()), 2)

    def test_upgrade_removes_partial_wrong_decoding_and_replays_shared_rows(self):
        # A cp1252 file can begin with valid UTF-8 bytes by coincidence. Simulate
        # an older run committing that prefix and then encountering bad UTF-8.
        source = self.root / "z.txt"
        source.write_bytes(b"\xc3\xa7\n\xe9\n")
        original = module._rows

        def partial(path, *args):
            if path.name == "z.txt":
                yield None, {"texto": "ç"}
                raise UnicodeDecodeError("utf-8", b"\xe7", 0, 1, "invalid")
            yield from original(path, *args)

        with patch.object(module, "_rows", side_effect=partial):
            with self.assertRaises(ConsolidationError):
                consolidate(self.root, batch_size=1)
        # On a retry a newly discovered earlier file shares the partial row.
        self.write("a.txt", "ç\n", "utf-8")
        with patch.object(module, "_rows", side_effect=partial):
            with self.assertRaises(ConsolidationError):
                consolidate(self.root, batch_size=1)
        events = []
        summary = consolidate(self.root, encoding="auto", remove_sources=True, batch_size=1, progress=events.append)
        self.assertEqual(summary["registros"], 3)
        self.assertEqual(summary["linhas_origem"], 3)
        self.assertEqual({row["texto"] for row in self.records()}, {b"\xc3\xa7".decode("cp1252"), "é", "ç"})
        self.assertTrue(any(event.get("reler_fontes") for event in events))
        with closing(sqlite3.connect(self.final)) as connection:
            self.assertEqual(connection.execute("SELECT sum(novos) FROM fontes").fetchone()[0], 3)

    def test_wrong_partial_interpretation_is_not_retained_as_an_extra_record(self):
        source = self.root / "a.txt"
        source.write_bytes(b"\xc3\xa7\n\xe9\n")

        def partial(*args):
            yield None, {"texto": "ç"}
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid")

        with patch.object(module, "_rows", side_effect=partial):
            with self.assertRaises(ConsolidationError):
                consolidate(self.root, batch_size=1)
        consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertEqual({row["texto"] for row in self.records()}, {b"\xc3\xa7".decode("cp1252"), "é"})

    def test_failed_policy_migration_rolls_back_records_counts_and_options(self):
        source = self.root / "a.txt"
        source.write_bytes(b"\xc3\xa7\n\xe9\n")

        def partial(*args):
            yield None, {"texto": "ç"}
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid")

        with patch.object(module, "_rows", side_effect=partial):
            with self.assertRaises(ConsolidationError):
                consolidate(self.root, batch_size=1)
        with closing(sqlite3.connect(self.pending)) as connection:
            connection.execute("CREATE TRIGGER falhar BEFORE UPDATE ON configuracao BEGIN SELECT RAISE(ABORT,'simulado'); END")
            connection.commit()
        with self.assertRaises(sqlite3.Error):
            consolidate(self.root, encoding="auto", remove_sources=True)
        with closing(sqlite3.connect(self.pending)) as connection:
            self.assertEqual(connection.execute("SELECT sum(novos) FROM fontes").fetchone()[0], 1)
            self.assertEqual(json.loads(connection.execute("SELECT dados FROM registros").fetchone()[0]), {"texto": "ç"})
            options = json.loads(connection.execute("SELECT valor FROM configuracao WHERE chave='opcoes'").fetchone()[0])
            self.assertEqual(options["encoding"], "utf-8-sig")
        self.assertTrue(source.exists())

    def test_legacy_database_without_encoding_column_can_resume(self):
        self.write("a.txt", "ação", "cp1252")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root)
        with closing(sqlite3.connect(self.pending)) as connection:
            connection.execute("ALTER TABLE fontes DROP COLUMN encoding")
            connection.commit()
        consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertEqual(self.records(), [{"texto": "ação"}])

    def test_upgrade_does_not_override_explicit_cp1252_policy(self):
        self.write("a.txt", "ação", "cp1252")

        def stop(event):
            if event["stage"] == "rows_committed":
                raise ConsolidationError("parar")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, encoding="cp1252", progress=stop)
        with self.assertRaisesRegex(ConsolidationError, "outra configuração"):
            consolidate(self.root, encoding="auto", remove_sources=True)

    def test_final_verified_database_policy_is_not_automatically_changed(self):
        source = self.write("a.txt", "ação", "utf-8")
        consolidate(self.root)
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, encoding="auto", remove_sources=True)
        self.assertTrue(source.exists())

    def test_encoding_policy_does_not_allow_delimiter_change(self):
        self.write("a.txt", "ação", "cp1252")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root)
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, encoding="auto", delimiter=";")

    def test_changed_original_blocks_reinterpretation_before_removing_rows(self):
        source = self.root / "a.txt"
        source.write_bytes(b"\xc3\xa7\n\xe7\n")

        def partial(*args):
            yield None, {"texto": "ç"}
            raise UnicodeDecodeError("utf-8", b"\xe7", 0, 1, "invalid")

        with patch.object(module, "_rows", side_effect=partial):
            with self.assertRaises(ConsolidationError):
                consolidate(self.root, batch_size=1)
        source.write_bytes(b"modificado\xe7\n")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, encoding="auto", remove_sources=True)
        with closing(sqlite3.connect(self.pending)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM registros").fetchone()[0], 1)
            saved = json.loads(connection.execute("SELECT valor FROM configuracao WHERE chave='opcoes'").fetchone()[0])
            self.assertEqual(saved["encoding"], "utf-8-sig")
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
