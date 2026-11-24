from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from leitor_dados import consolidate as module
from leitor_dados.consolidate import ConsolidationError, consolidate


class ConsolidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "Lote ficticio"
        self.root.mkdir()
        self.final = self.root / (self.root.name + ".sqlite")
        self.pending = self.root / ("." + self.root.name + ".importando.sqlite")

    def write(self, name, content, encoding="utf-8"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding, newline="")
        return path

    def rows(self):
        with closing(sqlite3.connect(self.final)) as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT dados FROM registros")]

    def test_global_whole_row_hash_across_folders_and_column_order(self):
        self.write("a/1.csv", "cpf;nome\n00000000000;Pessoa A\n00000000000;Pessoa B\n")
        self.write("b/2.csv", "nome;cpf\nPessoa A;00000000000\n")
        result = consolidate(self.root, remove_sources=True, batch_size=1)
        self.assertEqual(result["registros"], 2)
        self.assertEqual(result["linhas_origem"], 3)
        self.assertEqual({row["nome"] for row in self.rows()}, {"Pessoa A", "Pessoa B"})
        self.assertEqual(list(self.root.iterdir()), [self.final])
        with closing(sqlite3.connect(self.final)) as connection:
            for digest, data in connection.execute("SELECT hash,dados FROM registros"):
                self.assertEqual(digest, hashlib.sha256(data.encode()).digest())

    def test_txt_free_lines_preserve_blank_spaces_and_no_header(self):
        self.write("pasta/a.txt", "primeira\r\n\r\n  texto  \nprimeira\nultima")
        result = consolidate(self.root, remove_sources=True)
        self.assertEqual(result["linhas_origem"], 5)
        self.assertEqual({row["texto"] for row in self.rows()}, {"primeira", "", "  texto  ", "ultima"})

    def test_backup_is_not_read_or_changed(self):
        archive = self.write("backup/qualquer/sub/arquivo.rar", "nao ler")
        self.write("a.txt", "texto")
        consolidate(self.root, remove_sources=True)
        self.assertEqual(archive.read_text(), "nao ler")
        self.assertEqual({path.name for path in self.root.iterdir()}, {"backup", self.final.name})

    def test_default_preserves_sources_and_later_cleanup(self):
        source = self.write("pasta/a.txt", "texto")
        consolidate(self.root)
        self.assertTrue(source.exists())
        consolidate(self.root, remove_sources=True)
        self.assertFalse(source.exists())
        self.assertEqual(consolidate(self.root, remove_sources=True)["registros"], 1)

    def test_sqlite_all_tables_null_blob_and_types(self):
        source = self.root / "origem.db"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute('CREATE TABLE "vendas a" (codigo TEXT, valor, extra)')
            connection.executemany('INSERT INTO "vendas a" VALUES (?,?,?)', [("001", 1, None), ("001", "1", b"abc")])
            connection.execute("CREATE TABLE segunda (codigo TEXT, valor, extra)")
            connection.execute("INSERT INTO segunda VALUES ('001',1,NULL)")
            connection.commit()
        consolidate(self.root, remove_sources=True)
        self.assertEqual(len(self.rows()), 2)
        self.assertIn({"codigo": "001", "valor": 1, "extra": None}, self.rows())
        self.assertIn({"codigo": "001", "valor": "1", "extra": {"$sqlite_blob_base64": "YWJj"}}, self.rows())

    def test_csv_sqlite_text_records_deduplicate(self):
        self.write("a.csv", "codigo,nome\n001,Ficticio\n")
        with closing(sqlite3.connect(self.root / "b.db")) as connection:
            connection.execute("CREATE TABLE origem (nome TEXT,codigo TEXT)")
            connection.execute("INSERT INTO origem VALUES ('Ficticio','001')")
            connection.commit()
        self.assertEqual(consolidate(self.root, remove_sources=True)["registros"], 1)

    def test_failed_file_keeps_every_source_and_batches_can_resume(self):
        first = self.write("a.txt", "primeiro\nprimeiro\n")
        second = self.write("b.txt", "segundo\nterceiro\n")
        original_rows = module._rows

        def failing(path, *args):
            for number, row in enumerate(original_rows(path, *args)):
                yield row
                if path.name == "b.txt" and number == 0:
                    raise ConsolidationError("falha simulada")

        with patch.object(module, "_rows", side_effect=failing):
            with self.assertRaises(ConsolidationError):
                consolidate(self.root, remove_sources=True, batch_size=1)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse(self.final.exists())
        result = consolidate(self.root, remove_sources=True, batch_size=1)
        self.assertEqual(result["registros"], 3)
        self.assertEqual(result["linhas_origem"], 4)

    def test_changed_previously_imported_file_blocks_cleanup(self):
        source = self.write("a.txt", "um")
        consolidate(self.root)
        source.write_text("outro")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())

    def test_unsupported_file_blocks_before_database_creation(self):
        source = self.write("a.txt", "um")
        other = self.write("documento.zip", "arquivo")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(other.exists())
        self.assertTrue(source.exists())
        self.assertFalse(self.pending.exists())

    def test_malformed_csv_preserves_sources_and_no_final(self):
        source = self.write("pasta/a.csv", "id;nome\n1;nome\n2;nome;extra\n")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True, delimiter=";", batch_size=1)
        self.assertTrue(source.exists())
        self.assertFalse(self.final.exists())

    def test_duplicate_csv_header_fails(self):
        self.write("a.csv", "id,id\n1,2\n")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)

    def test_csv_multiline_quoted_field_preserved(self):
        self.write("a.csv", 'id;nome\n001;"linha1\nlinha2"\n')
        consolidate(self.root, remove_sources=True)
        self.assertEqual(self.rows(), [{"id": "001", "nome": "linha1\nlinha2"}])

    def test_encoding_strict_then_explicit_cp1252(self):
        source = self.write("a.txt", "ação", encoding="cp1252")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())
        # A different decoding cannot be applied to a database carrying a different configuration.
        self.pending.unlink()
        consolidate(self.root, remove_sources=True, encoding="cp1252")
        self.assertEqual(self.rows(), [{"texto": "ação"}])

    def test_existing_unrelated_final_is_not_modified(self):
        self.write("a.txt", "um")
        with closing(sqlite3.connect(self.final)) as connection:
            connection.execute("CREATE TABLE importante (id)")
        before = self.final.read_bytes()
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertEqual(before, self.final.read_bytes())

    def test_invalid_db_and_sqlite_view_block_removal(self):
        source = self.write("a.db", "nao e sqlite")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())
        self.pending.unlink()
        source.unlink()
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE itens (id TEXT)")
            connection.execute("CREATE VIEW vista AS SELECT * FROM itens")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())

    def test_corrupt_record_hash_blocks_cleanup(self):
        source = self.write("a.txt", "um")
        consolidate(self.root)
        with closing(sqlite3.connect(self.final)) as connection:
            connection.execute("UPDATE registros SET dados='{}'")
            connection.commit()
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())

    def test_new_file_before_cleanup_blocks_all_deletions(self):
        source = self.write("a.txt", "um")
        triggered = False

        def progress(event):
            nonlocal triggered
            if event["stage"] == "database_verified" and not triggered:
                triggered = True
                self.write("novo.txt", "chegou agora")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True, progress=progress)
        self.assertTrue(source.exists())
        self.assertTrue((self.root / "novo.txt").exists())

    def test_cleanup_interruption_can_resume_from_final_database(self):
        self.write("pasta/a.txt", "um")
        self.write("pasta/b.txt", "dois")

        def progress(event):
            if event["stage"] == "source_removed":
                raise ConsolidationError("interrupcao simulada")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True, progress=progress)
        self.assertTrue(self.final.exists())
        self.assertEqual(len(self.rows()), 2)
        consolidate(self.root, remove_sources=True)
        self.assertEqual(list(self.root.iterdir()), [self.final])

    def test_nested_backup_and_mount_are_refused_before_writes(self):
        self.write("pasta/backup/a.rar", "arquivo")
        self.write("a.txt", "um")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertFalse(self.pending.exists())
        with patch.object(module.os.path, "ismount", return_value=True):
            with self.assertRaises(ConsolidationError):
                consolidate(self.root, remove_sources=True)

    def test_symlink_refused(self):
        source = self.write("a.txt", "um")
        link = self.root / "b.txt"
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest("Symlinks unavailable on this Windows environment")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)

    def test_changed_folder_identity_before_cleanup_is_refused(self):
        self.write("pasta/a.txt", "um")
        consolidate(self.root)
        with closing(sqlite3.connect(self.final)) as connection:
            connection.execute("UPDATE pastas SET identidade='[0,0]'")
            connection.commit()
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue((self.root / "pasta/a.txt").exists())

    def test_incomplete_sources_prevent_verification(self):
        source = self.write("a.txt", "um")
        consolidate(self.root)
        with closing(sqlite3.connect(self.final)) as connection:
            connection.execute("UPDATE fontes SET completo=0")
            connection.commit()
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())

    def test_lock_does_not_get_removed_by_second_process(self):
        (self.root / module.LOCK).mkdir()
        self.write("a.txt", "um")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue((self.root / module.LOCK).exists())

    def test_blank_csv_line_fails_instead_of_silently_skipping(self):
        self.write("a.csv", "id,nome\n1,pessoa\n\n")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)

    def test_command_runs_from_current_folder_without_docker(self):
        self.write("sub/a.csv", "codigo,nome\n001,Registro ficticio\n")
        script = Path(__file__).resolve().parents[1] / "scripts" / "consolidar_sqlite.py"
        process = subprocess.run([sys.executable, str(script), "--apagar-fontes", "--encoding", "auto",
                                  "--cache-mib", "16", "--batch-size", "2", "--batch-mib", "4", "--reorganizar"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = json.loads(process.stdout.splitlines()[-1])
        self.assertEqual(summary["registros"], 1)
        self.assertTrue(summary["fontes_removidas"])
        self.assertEqual(list(self.root.iterdir()), [self.final])
        self.assertNotIn("Registro ficticio", process.stdout)

    def test_hash_collision_aborts_instead_of_dropping_record(self):
        source = self.write("a.txt", "um\ndois")

        class ConstantHash:
            def __init__(self, *args):
                pass

            def update(self, value):
                pass

            def hexdigest(self):
                return "00" * 32

            def digest(self):
                return b"\x00" * 32

        with patch.object(module.hashlib, "sha256", ConstantHash):
            with self.assertRaisesRegex(ConsolidationError, "Colisão"):
                consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())
        self.assertFalse(self.final.exists())

    def test_output_sync_failure_keeps_sources(self):
        source = self.write("pasta/a.txt", "um")
        with patch.object(module, "_sync_output", side_effect=OSError("falha simulada")):
            with self.assertRaises(OSError):
                consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())
        self.assertFalse(self.final.exists())

    def test_missing_source_before_cleanup_blocks_other_deletions(self):
        source = self.write("a.txt", "um")
        other = self.write("b.txt", "dois")
        consolidate(self.root)
        other.unlink()
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())

    def test_changed_source_during_import_never_gets_deleted(self):
        source = self.write("a.txt", "um\ndois\n")
        changed = False

        def progress(event):
            nonlocal changed
            if event["stage"] == "rows_committed" and not changed:
                changed = True
                with source.open("a") as stream:
                    stream.write("tres\n")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True, batch_size=1, progress=progress)
        self.assertTrue(source.exists())
        self.assertFalse(self.final.exists())

    def test_orphan_journal_is_not_silently_left_after_cleanup(self):
        source = self.write("a.txt", "um")
        journal = self.root / (self.final.name + "-journal")
        journal.write_bytes(b"ficticio")
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())

    def test_record_count_reconciliation_blocks_cleanup(self):
        source = self.write("a.txt", "um")
        consolidate(self.root)
        with closing(sqlite3.connect(self.final)) as connection:
            connection.execute("UPDATE fontes SET novos=0")
            connection.commit()
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
