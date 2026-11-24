from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from leitor_dados import consolidate as module
from leitor_dados.consolidate import ConsolidationError, consolidate, _BatchWriter
from scripts.benchmark_consolidacao import benchmark


class ConsolidationPerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "LoteTeste"
        self.root.mkdir()
        self.final = self.root / "LoteTeste.sqlite"
        self.pending = self.root / ".LoteTeste.importando.sqlite"

    def write(self, name, value):
        path = self.root / name
        path.write_text(value, encoding="utf-8", newline="")
        return path

    def test_csv_resume_skips_hash_and_serialization_of_committed_prefix(self):
        self.write("a.csv", "codigo,nome\n001,Exemplo A\n002,Exemplo B\n003,Exemplo C\n")

        def interrupt(event):
            if event["stage"] == "rows_committed":
                raise ConsolidationError("interromper")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, batch_size=2, progress=interrupt)
        original = module._json
        encoded_rows = []

        def capture(value):
            if isinstance(value, dict) and "codigo" in value:
                encoded_rows.append(value["codigo"])
            return original(value)

        events = []
        with patch.object(module, "_json", side_effect=capture):
            summary = consolidate(self.root, remove_sources=True, batch_size=2, progress=events.append)
        self.assertEqual(encoded_rows, ["003"])
        self.assertEqual(summary["registros"], 3)
        self.assertEqual(summary["linhas_origem"], 3)
        self.assertTrue(any(event["stage"] == "source_resume_ready" and event["linhas"] == 2 for event in events))

    def test_resume_preserves_duplicates_counts_and_budget_can_change(self):
        self.write("a.txt", "um\ndois\num\ntres\n")

        def interrupt(event):
            if event["stage"] == "rows_committed":
                raise ConsolidationError("interromper")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, batch_size=2, cache_mib=16, progress=interrupt)
        summary = consolidate(self.root, remove_sources=True, batch_size=3, cache_mib=128, batch_mib=8)
        self.assertEqual(summary["linhas_origem"], 4)
        self.assertEqual(summary["registros"], 3)

    def test_checkpoint_past_end_of_file_blocks_cleanup(self):
        source = self.write("a.txt", "um\ndois\n")

        def interrupt(event):
            if event["stage"] == "rows_committed":
                raise ConsolidationError("interromper")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, batch_size=1, progress=interrupt)
        with closing(sqlite3.connect(self.pending)) as connection:
            connection.execute("UPDATE fontes SET linhas=99")
            connection.commit()
        with self.assertRaisesRegex(ConsolidationError, "ponto de retomada"):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(source.exists())

    def test_payload_budget_commits_before_row_count_limit(self):
        source = self.write("a.txt", "a" * (2*1024**2) + "\n" + "b" * (2*1024**2) + "\n" + "c\n")
        events = []

        def interrupt(event):
            events.append(event)
            if event["stage"] == "rows_committed":
                raise ConsolidationError("interromper")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, batch_size=100000, batch_mib=4, progress=interrupt)
        committed = [event for event in events if event["stage"] == "rows_committed"]
        self.assertEqual(committed[0]["linhas_lidas"], 2)
        self.assertTrue(source.exists())
        self.assertEqual(consolidate(self.root, remove_sources=True, batch_mib=4)["registros"], 3)

    def test_totals_report_all_files_instead_of_only_current_file(self):
        self.write("a.txt", "um\ndois\ntres\n")
        self.write("b.txt", "um\nquatro\n")
        events = []
        consolidate(self.root, remove_sources=True, batch_size=2, progress=events.append)
        last = [event for event in events if event["stage"] == "rows_committed"][-1]
        self.assertEqual(last["linhas_lidas"], 2)
        self.assertEqual(last["linhas_total_confirmadas"], 5)
        self.assertEqual(last["registros_unicos_total"], 4)
        self.assertGreater(last["linhas_por_segundo"], 0)

    def test_batch_duplicate_keeps_first_occurrence_and_detects_existing_collision(self):
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("CREATE TABLE registros(hash BLOB PRIMARY KEY,dados TEXT NOT NULL,fonte_id INTEGER,tabela_origem TEXT) WITHOUT ROWID")
            writer = _BatchWriter(connection)
            digest = b"0" * 32
            self.assertEqual(writer.write({digest: ('{"texto":"um"}', "primeira")}, 1), 1)
            self.assertEqual(writer.write({digest: ('{"texto":"um"}', "outra")}, 2), 0)
            self.assertEqual(connection.execute("SELECT fonte_id,tabela_origem FROM registros").fetchone(), (1, "primeira"))
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ConsolidationError, "Colisão"):
                writer.write({b"1" * 32: ('{"texto":"dois"}', None), digest: ('{"texto":"diferente"}', None)}, 3)
            connection.rollback()
            self.assertEqual(connection.execute("SELECT count(*) FROM registros").fetchone()[0], 1)
            self.assertEqual(json.loads(connection.execute("SELECT dados FROM registros").fetchone()[0]), {"texto": "um"})

    def test_runtime_limits_are_validated_before_creating_database(self):
        self.write("a.txt", "um")
        for options in ({"cache_mib": 0}, {"cache_mib": 20000}, {"batch_mib": 0}, {"batch_mib": True}):
            with self.subTest(options=options), self.assertRaises(ConsolidationError):
                consolidate(self.root, **options)
        self.assertFalse(self.pending.exists())

    def test_tuning_retains_full_sync_and_configures_requested_cache(self):
        self.write("a.txt", "um")
        original = module._import_source
        pragmas = []

        def inspect(connection, *args, **kwargs):
            pragmas.append((connection.execute("PRAGMA cache_size").fetchone()[0],
                            connection.execute("PRAGMA synchronous").fetchone()[0],
                            connection.execute("PRAGMA journal_mode").fetchone()[0]))
            return original(connection, *args, **kwargs)

        with patch.object(module, "_import_source", side_effect=inspect):
            consolidate(self.root, remove_sources=True, cache_mib=128)
        self.assertEqual(pragmas, [(-128*1024, 2, "delete")])

    def test_benchmark_engines_produce_identical_results_without_time_assertion(self):
        report = benchmark(self.root, rows=100, payload_bytes=32, repeats=1, cache_mib=16, batch_size=20, compare_layouts=True)
        self.assertEqual({run["unique"] for run in report["runs"]}, {75})
        self.assertEqual(len({run["fingerprint"] for run in report["runs"]}), 1)
        self.assertGreater(report["speedup"], 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def legacy_pending(self):
        source = self.write("a.txt", "um\ndois\ntres\n")

        def interrupt(event):
            if event["stage"] == "rows_committed":
                raise ConsolidationError("interromper")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, batch_size=2, progress=interrupt)
        with closing(sqlite3.connect(self.pending)) as connection:
            connection.executescript('''BEGIN;
                CREATE TABLE antigo(hash BLOB PRIMARY KEY CHECK(length(hash)=32), dados TEXT NOT NULL,
                    fonte_id INTEGER NOT NULL,tabela_origem TEXT) WITHOUT ROWID;
                INSERT INTO antigo SELECT hash,dados,fonte_id,tabela_origem FROM registros;
                DROP TABLE registros;
                ALTER TABLE antigo RENAME TO registros;
                COMMIT;''')
        return source

    def test_legacy_layout_reorganization_preserves_hashes_data_provenance_and_checkpoint(self):
        self.legacy_pending()
        with closing(sqlite3.connect(self.pending)) as connection:
            previous = set(connection.execute("SELECT hash,dados,fonte_id,tabela_origem FROM registros"))
        events = []
        summary = consolidate(self.root, remove_sources=True, reorganize=True, progress=events.append)
        self.assertEqual(summary["registros"], 3)
        with closing(sqlite3.connect(self.final)) as connection:
            actual = set(connection.execute("SELECT hash,dados,fonte_id,tabela_origem FROM registros"))
            info = next(row for row in connection.execute("PRAGMA table_list") if row[1] == "registros")
            self.assertEqual(info[4], 0)
            self.assertTrue(previous.issubset(actual))
            self.assertEqual(connection.execute("SELECT count(DISTINCT id) FROM registros").fetchone()[0], 3)
        self.assertTrue(any(event["stage"] == "database_reorganized" for event in events))
        self.assertTrue(any(event["stage"] == "source_resume_ready" and event["linhas"] == 2 for event in events))

    def test_interrupted_reorganization_rolls_back_to_legacy_and_can_retry(self):
        source = self.legacy_pending()

        def interrupt(event):
            if event["stage"] == "building_compact_hash_index":
                raise ConsolidationError("interromper reorganizacao")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True, reorganize=True, progress=interrupt)
        self.assertTrue(source.exists())
        with closing(sqlite3.connect(self.pending)) as connection:
            info = next(row for row in connection.execute("PRAGMA table_list") if row[1] == "registros")
            self.assertEqual(info[4], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM registros").fetchone()[0], 2)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_schema WHERE name='_ld_registros_v2'").fetchone())
        self.assertEqual(consolidate(self.root, reorganize=True, remove_sources=True)["registros"], 3)

    def test_reorganization_refuses_insufficient_space_before_copying(self):
        source = self.legacy_pending()
        with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ConsolidationError, "Espaço insuficiente"):
                consolidate(self.root, reorganize=True, remove_sources=True)
        self.assertTrue(source.exists())

    def test_reorganization_count_mismatch_keeps_original_table(self):
        source = self.legacy_pending()
        with closing(sqlite3.connect(self.pending)) as connection:
            connection.execute("UPDATE fontes SET novos=0")
            connection.commit()
        with self.assertRaisesRegex(ConsolidationError, "Contagem inconsistente"):
            consolidate(self.root, reorganize=True, remove_sources=True)
        with closing(sqlite3.connect(self.pending)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM registros").fetchone()[0], 2)
        self.assertTrue(source.exists())

    def test_new_layout_does_not_reorganize_again(self):
        self.write("a.txt", "um")
        events = []
        consolidate(self.root, reorganize=True, progress=events.append)
        self.assertTrue(any(event["stage"] == "database_layout_current" for event in events))
        self.assertFalse(any(event["stage"] == "reorganizing_database" for event in events))

    def test_published_database_is_not_reorganized(self):
        source = self.write("a.txt", "um")
        consolidate(self.root)
        before = self.final.read_bytes()
        with self.assertRaises(ConsolidationError):
            consolidate(self.root, reorganize=True, remove_sources=True)
        self.assertEqual(self.final.read_bytes(), before)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
