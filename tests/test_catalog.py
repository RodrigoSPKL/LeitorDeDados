"""Grouping, append semantics, safe SQLite reads and per-source rollback."""

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from leitor_dados.catalog import CatalogError, import_files


@dataclass
class Source:
    path: Path
    logical_parent: str
    source: str


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.catalog = self.base / "catalog.sqlite"

    def csv(self, name, contents, folder="vendas"):
        path = self.base / name
        path.write_text(contents, encoding="utf-8", newline="\n")
        return Source(path, folder, folder + "/" + name)

    def rows(self, table):
        with closing(sqlite3.connect(self.catalog)) as connection:
            return connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"').fetchall()

    def test_parts_with_same_columns_merge_in_any_order_and_keep_duplicates(self):
        first = self.csv("parte1.csv", "CPF;Nome\n00123456789;Ana\n00123456789;Ana\n")
        second = self.csv("parte2.csv", " nome ;cpf\nBia;00000000000\n")
        third = self.csv("parte3.tsv", "CPF\tNOME\n00123456789\tAna\n")
        counts = []
        report = import_files([first, second, third], self.catalog, progress=counts.append)
        self.assertEqual((report["imported_files"], report["imported_rows"]), (3, 4))
        self.assertEqual(counts, [2, 3, 4])
        self.assertEqual(len(report["tables"]), 1)
        table = report["tables"][0]
        self.assertEqual(table["columns"], ["cpf", "nome"])
        self.assertEqual(table["fields"], {"cpf": "cpf"})
        self.assertEqual(table["rows"], 4)
        self.assertEqual(self.rows(table["name"]).count(("00123456789", "Ana")), 3)

    def test_different_folders_or_columns_are_separate_and_identifiers_are_quoted(self):
        files = [self.csv("one.csv", "cpf,nome\n00123456789,Ana\n", "folder1"),
                 self.csv("two.csv", "cpf,nome\n00123456789,Ana\n", "folder2"),
                 self.csv("three.csv", 'cpf,"odd""; DROP TABLE test;--"\n00123456789,safe\n', "folder1")]
        report = import_files(files, self.catalog)
        self.assertEqual(len(report["tables"]), 3)
        self.assertEqual(len({table["name"] for table in report["tables"]}), 3)
        self.assertEqual(sum(table["rows"] for table in report["tables"]), 3)
        self.assertTrue(all(len(self.rows(table["name"])) == 1 for table in report["tables"]))

    def test_ledger_skips_unchanged_source_but_changed_source_appends_in_full(self):
        first = self.csv("one.csv", "cpf,nome\n00123456789,Ana\n")
        report = import_files([first], self.catalog)
        table_name = report["tables"][0]["name"]
        again = import_files([first], self.catalog)
        self.assertEqual((again["imported_files"], again["skipped_files"], again["imported_rows"]), (0, 1, 0))
        self.assertEqual(again["tables"][0]["rows"], 1)
        first.path.write_text("cpf,nome\n00123456789,Ana\n00000000000,Bia\n", encoding="utf-8")
        changed = import_files([first], self.catalog)
        self.assertEqual(changed["imported_rows"], 2)
        self.assertEqual(changed["tables"][0]["rows"], 3)
        self.assertEqual(self.rows(table_name).count(("00123456789", "Ana")), 2)

    def test_malformed_source_rolls_back_its_rows_without_losing_prior_sources(self):
        good = self.csv("good.csv", "cpf,nome\n00123456789,Ana\n")
        bad = self.csv("bad.csv", "cpf,nome\n00000000000,Bia\nprivate,wrong,extra\n")
        with self.assertRaises(CatalogError) as context:
            import_files([good, bad], self.catalog, options={"batch_size": 1})
        self.assertNotIn("private", str(context.exception))
        self.assertNotIn(str(bad.path), str(context.exception))
        self.assertEqual(context.exception.details, {
            "source": "vendas/bad.csv", "format": "delimited", "record": 2,
            "line_start": 3, "line_end": 3, "expected_columns": 2,
            "actual_columns": 3, "delimiter": ",",
        })
        report = import_files([good], self.catalog)
        self.assertEqual(report["skipped_files"], 1)
        self.assertEqual(report["tables"][0]["rows"], 1)
        self.assertEqual(self.rows(report["tables"][0]["name"]), [("00123456789", "Ana")])

    def test_csv_diagnostics_track_multiline_records_and_blank_lines_without_values(self):
        source = self.csv("multiline.csv", 'cpf,nome\n00123456789,"private-first\nprivate-second"\n\nprivate,extra,values\n')
        with self.assertRaises(CatalogError) as caught:
            import_files([source], self.catalog, options={"delimiter": ",", "batch_size": 1})
        details = caught.exception.details
        self.assertEqual((details["record"], details["line_start"], details["line_end"]), (2, 5, 5))
        self.assertEqual((details["expected_columns"], details["actual_columns"]), (2, 3))
        self.assertNotIn("private", str(caught.exception) + json.dumps(details))
        self.assertEqual(import_files([], self.catalog)["tables"], [])

    def test_unclosed_quotes_have_source_and_physical_lines_without_input_values(self):
        source = self.csv("quotes.csv", 'cpf,nome\n00123456789,"private-first\nprivate-second\n')
        with self.assertRaises(CatalogError) as caught:
            import_files([source], self.catalog, options={"delimiter": ","})
        details = caught.exception.details
        self.assertEqual((details["record"], details["line_start"], details["line_end"]), (1, 2, 3))
        self.assertEqual(details["source"], source.source)
        self.assertNotIn("private", str(caught.exception) + json.dumps(details))

    def test_explicit_separator_can_recover_failed_file_without_changing_source(self):
        source = self.csv("separator.csv", "cpf;nome\n00123456789;private,last\n")
        original = source.path.read_bytes()
        with self.assertRaises(CatalogError) as caught:
            import_files([source], self.catalog, options={"delimiter": ","})
        self.assertEqual(caught.exception.details["expected_columns"], 1)
        recovered = import_files([source], self.catalog, options={"delimiter": ";"})
        self.assertEqual(recovered["imported_rows"], 1)
        self.assertEqual(self.rows(recovered["tables"][0]["name"]), [("00123456789", "private,last")])
        self.assertEqual(source.path.read_bytes(), original)

    def test_sniffer_fallback_keeps_semicolon_and_respects_quoted_header(self):
        for index, header in enumerate(("cpf;nome", 'cpf;"nome;apelido"')):
            with self.subTest(header=header):
                source = self.csv(f"bad-semicolon-{index}.csv",
                                  header + "\n00123456789;Ana\nprivate;extra;columns\n")
                with self.assertRaises(CatalogError):
                    import_files([source], self.catalog, options={"batch_size": 1})
        self.assertEqual(import_files([], self.catalog)["tables"], [])

    def test_sniffer_fallback_rejects_tied_headers_but_accepts_single_column(self):
        ambiguous = self.csv("ambiguous.csv", "one,two;three\nA,B;C\nD,E,F;extra;too\n")
        with self.assertRaisesRegex(CatalogError, "ambíguo"):
            import_files([ambiguous], self.catalog)
        single = self.csv("single.csv", "description\nhello\nworld\n")
        report = import_files([single], self.catalog)
        self.assertEqual(report["tables"][0]["columns"], ["description"])
        self.assertEqual(report["tables"][0]["rows"], 2)

    def make_db(self, name, value, customer_table="customers"):
        path = self.base / name
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f'CREATE TABLE "{customer_table}" (cpf TEXT, data BLOB)')
            connection.execute(f'INSERT INTO "{customer_table}" VALUES (?,?)', (value, b"\x00\xff"))
            connection.execute('CREATE TABLE "sales" (cpf TEXT, data BLOB)')
            connection.execute('INSERT INTO "sales" VALUES (?,?)', (value, None))
            connection.commit()
        return Source(path, "databases", "databases/" + name)

    def test_sqlite_parts_merge_by_source_table_and_source_is_unchanged(self):
        first = self.make_db("part1.db", "00123456789")
        second = self.make_db("part2.db", "00000000000", customer_table="Customers")
        before = {source.path: source.path.read_bytes() for source in (first, second)}
        report = import_files([first, second], self.catalog)
        self.assertEqual(report["imported_rows"], 4)
        self.assertEqual(len(report["tables"]), 2)
        self.assertEqual({table["source_table"] for table in report["tables"]}, {"customers", "sales"})
        customers = next(table for table in report["tables"] if table["source_table"] == "customers")
        self.assertEqual(customers["rows"], 2)
        self.assertEqual(json.loads(self.rows(customers["name"])[0][1]), {"$binary": "base64", "data": "AP8="})
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertFalse(Path(str(path) + "-journal").exists())
            self.assertFalse(Path(str(path) + "-wal").exists())

    def test_jsonl_schema_is_consistent_and_nested_values_become_json_text(self):
        path = self.base / "records.jsonl"
        path.write_text('{"CPF":"00123456789","extra":{"b":2,"a":1}}\n'
                        '{"extra":null,"cpf":"00000000000"}\n', encoding="utf-8")
        source = Source(path, "people", "people/records.jsonl")
        report = import_files([source], self.catalog)
        table = report["tables"][0]
        self.assertEqual(self.rows(table["name"]), [("00123456789", '{"a":1,"b":2}'), ("00000000000", None)])
        path.write_text('{"cpf":"private"}\n{"different":"value"}\n', encoding="utf-8")
        with self.assertRaises(CatalogError):
            import_files([source], self.catalog)
        self.assertEqual(len(self.rows(table["name"])), 2)

    def test_rejects_duplicate_headers_unmanaged_destination_and_self_import(self):
        duplicate = self.csv("duplicate.csv", "CPF, cpf \n00123456789,00000000000\n")
        with self.assertRaises(CatalogError):
            import_files([duplicate], self.catalog)
        with self.assertRaises(CatalogError):
            import_files([Source(self.catalog, "self", "self")], self.catalog)
        other = self.base / "unmanaged.sqlite"
        with closing(sqlite3.connect(other)) as connection:
            connection.execute("CREATE TABLE important (value TEXT)")
            connection.commit()
        original = other.read_bytes()
        with self.assertRaises(CatalogError):
            import_files([], other)
        self.assertEqual(other.read_bytes(), original)

    def test_rejects_wal_mode_source_and_explicit_journal(self):
        source = self.make_db("wal.db", "00123456789")
        with closing(sqlite3.connect(source.path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        with self.assertRaisesRegex(CatalogError, "checkpoint/DELETE"):
            import_files([source], self.catalog)
        normal = self.make_db("normal.db", "00123456789")
        Path(str(normal.path) + "-journal").write_bytes(b"")
        with self.assertRaisesRegex(CatalogError, "checkpoint/DELETE"):
            import_files([normal], self.catalog)

    def test_catalog_page_size_is_set_only_for_new_catalogs(self):
        first = import_files([], self.catalog)
        self.assertEqual(first["catalog_page_size"], 16384)
        explicit = self.base / "larger.sqlite"
        report = import_files([], explicit, options={"page_size": 32768})
        self.assertEqual(report["catalog_page_size"], 32768)
        self.assertEqual(import_files([], explicit)["catalog_page_size"], 32768)
        before = explicit.read_bytes()
        with self.assertRaisesRegex(CatalogError, "page_size"):
            import_files([], explicit, options={"page_size": 4096})
        self.assertEqual(explicit.read_bytes(), before)
        for value in (True, 256, 1000, 131072):
            with self.subTest(page_size=value):
                with self.assertRaises(CatalogError):
                    import_files([], self.base / "invalid.sqlite", options={"page_size": value})

    def test_sqlite_tables_colliding_after_casefold_are_rejected_without_data_loss(self):
        source = self.base / "collision.db"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute('CREATE TABLE "Straße" (cpf TEXT)')
            connection.execute('CREATE TABLE "STRASSE" (cpf TEXT)')
            connection.execute('INSERT INTO "Straße" VALUES ("00123456789")')
            connection.execute('INSERT INTO "STRASSE" VALUES ("00000000000")')
            connection.commit()
        with self.assertRaisesRegex(CatalogError, "nomes equivalentes"):
            import_files([Source(source, "dbs", "dbs/collision.db")], self.catalog)
        report = import_files([], self.catalog)
        self.assertEqual(report["tables"], [])


if __name__ == "__main__":
    unittest.main()
