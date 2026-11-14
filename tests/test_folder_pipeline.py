"""End-to-end folder imports, archive discovery, immutable publication and resume."""

from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
import zipfile

from leitor_dados.catalog import CatalogError
from leitor_dados.folder_pipeline import ingest_folder
from leitor_dados.query import DatasetStore


CPF = "00123456789"
SECRET = "pipeline-tests-only-" + "x" * 32
CONFIG = {"catalog": {"batch_size": 2},
          "snapshot": {"data_shards": 2, "lookup_shards": 3, "batch_size": 2}}


class FolderPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.folder = self.base / "input"
        self.folder.mkdir()
        self.workspace = self.base / "work"
        self.data = self.base / "data"

    def write(self, relative, text):
        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def ingest(self, version=None):
        return ingest_folder(self.folder, workspace=self.workspace, data_dir=self.data,
                             config=CONFIG, version=version)

    def store(self):
        return DatasetStore(self.data, SECRET)

    def manifest(self, version):
        return json.loads((self.data / "versions" / version / "manifest.json").read_text(encoding="utf-8"))

    def hashes(self, directory):
        return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()}

    def all_payloads(self, version):
        directory = self.data / "versions" / version
        result = []
        for shard in sorted(directory.glob("records-*.sqlite")):
            with closing(sqlite3.connect(shard)) as connection:
                result.extend(json.loads(row[0]) for row in connection.execute("SELECT payload FROM records"))
        return result

    def test_folder_parts_merge_and_distinct_folders_or_schemas_stay_separate(self):
        self.write("pessoas/1.csv", f"cpf;codigo;nome\n{CPF};P;Ana\n{CPF};P;Ana\n")
        self.write("pessoas/2.csv", f" Nome ;CPF;CODIGO\nAna;{CPF};P\n")
        self.write("outras/1.csv", f"cpf;codigo;nome\n{CPF};P;Bia\n")
        self.write("pessoas/extra.csv", f"cpf;codigo;nome;cidade\n{CPF};P;Caio;Recife\n")
        original = self.hashes(self.folder)
        report = self.ingest("grouped-v1")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["imported_rows"], 5)
        self.assertEqual(len(report["tables"]), 3)
        merged = next(table for table in report["tables"]
                      if table["logical_parent"] == "pessoas" and len(table["columns"]) == 3)
        self.assertEqual(merged["rows"], 3)
        matches = self.store().search("cpf", CPF, 100)["items"]
        self.assertEqual(len(matches), 5)
        duplicate_matches = [item for item in matches if item["data"]["_ld"]["table"] == merged["name"]]
        self.assertEqual(len(duplicate_matches), 3)
        self.assertTrue(all(item["data"]["record"]["nome"] == "Ana" for item in duplicate_matches))
        self.assertEqual({item["data"]["_ld"]["folder"] for item in matches}, {"pessoas", "outras"})
        self.assertEqual(self.hashes(self.folder), original)

    def test_unchanged_import_skips_rebuild_and_append_preserves_old_cursor(self):
        self.write("pessoas/1.csv", f"cpf;codigo;nome\n{CPF};P;Ana\n{CPF};P;Ana\n")
        self.write("pessoas/2.csv", f"nome;cpf;codigo\nBia;{CPF};P\n")
        self.ingest("stable-v1")
        store = self.store()
        first = store.search("cpf", CPF, 1)
        old_hashes = self.hashes(self.data / "versions" / "stable-v1")
        unchanged = self.ingest()
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertFalse(unchanged["snapshot_rebuilt"])
        self.assertEqual((unchanged["imported_files"], unchanged["skipped_files"], unchanged["imported_rows"]), (0, 2, 0))
        self.assertEqual(unchanged["tables"][0]["rows"], 3)
        self.write("pessoas/3.csv", f"cpf;codigo;nome\n{CPF};P;Caio\n")
        appended = self.ingest("stable-v2")
        self.assertEqual((appended["imported_files"], appended["imported_rows"]), (1, 1))
        self.assertEqual(appended["tables"][0]["rows"], 4)
        self.assertEqual(len(store.search("cpf", CPF, 100)["items"]), 4)
        old_items = first["items"][:]
        cursor = first["next_cursor"]
        while cursor:
            page = store.search("cpf", CPF, 1, cursor)
            self.assertEqual(page["version"], "stable-v1")
            old_items.extend(page["items"])
            cursor = page["next_cursor"]
        self.assertEqual([item["record_id"] for item in old_items], [1, 2, 3])
        self.assertEqual(self.hashes(self.data / "versions" / "stable-v1"), old_hashes)

    def test_failed_new_file_keeps_current_and_resumes_committed_catalog_files_once(self):
        first_source = self.write("pessoas/1.csv", f"cpf;codigo;nome\n{CPF};P;Ana\n")
        self.ingest("safe-v1")
        current = (self.data / "current.json").read_bytes()
        snapshot = self.hashes(self.data / "versions" / "safe-v1")
        first_hash = hashlib.sha256(first_source.read_bytes()).hexdigest()
        self.write("pessoas/2.csv", f"cpf;codigo;nome\n{CPF};P;Bia\n")
        self.write("pessoas/3.csv", f"cpf;codigo;nome\n{CPF};P;Caio\nprivate-marker;too;many;columns\n")
        with self.assertRaises(CatalogError) as context:
            self.ingest("failed-version")
        self.assertNotIn("private-marker", str(context.exception))
        self.assertEqual((self.data / "current.json").read_bytes(), current)
        self.assertEqual(self.hashes(self.data / "versions" / "safe-v1"), snapshot)
        self.assertFalse((self.data / "versions" / "failed-version").exists())
        self.assertEqual(len(self.store().search("cpf", CPF)["items"]), 1)
        self.write("pessoas/3.csv", f"cpf;codigo;nome\n{CPF};P;Caio\n")
        recovered = self.ingest("safe-v2")
        self.assertEqual((recovered["imported_files"], recovered["skipped_files"], recovered["imported_rows"]), (1, 2, 1))
        self.assertEqual(recovered["tables"][0]["rows"], 3)
        self.assertEqual(len(self.store().search("cpf", CPF, 100)["items"]), 3)
        self.assertEqual(hashlib.sha256(first_source.read_bytes()).hexdigest(), first_hash)

    @staticmethod
    def zip_bytes(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    @staticmethod
    def tar_bytes(entries):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    def test_nested_zip_tar_inputs_and_database_parts_publish_with_metadata(self):
        inner_zip = self.zip_bytes({"people/2.csv": f"nome;codigo;cpf\nBia;P;{CPF}\n".encode()})
        tar_content = self.tar_bytes({"people/1.csv": f"cpf;codigo;nome\n{CPF};P;Ana\n".encode(), "more.zip": inner_zip})
        (self.folder / "bundle.zip").write_bytes(self.zip_bytes({"release.tar": tar_content}))
        database_folder = self.folder / "dbs"
        database_folder.mkdir()
        for part, table_name in ((1, "users"), (2, "Users")):
            with closing(sqlite3.connect(database_folder / f"{part}.db")) as connection:
                connection.execute(f'CREATE TABLE "{table_name}" (cpf TEXT, codigo TEXT, nome TEXT, binary_data BLOB)')
                connection.execute(f'INSERT INTO "{table_name}" VALUES (?,?,?,?)', (CPF, "DB", f"person-{part}", b"\x00\xff"))
                connection.commit()
        original = self.hashes(self.folder)
        report = self.ingest("archives-v1")
        self.assertEqual(report["imported_rows"], 4)
        self.assertEqual(len(report["tables"]), 2)
        self.assertEqual({table["logical_parent"] for table in report["tables"]}, {"people", "dbs"})
        db_table = next(table for table in report["tables"] if table["logical_parent"] == "dbs")
        self.assertEqual((db_table["source_table"], db_table["rows"]), ("users", 2))
        items = self.store().search("cpf", CPF, 100)["items"]
        self.assertEqual(len(items), 4)
        self.assertEqual({item["data"]["_ld"]["folder"] for item in items}, {"people", "dbs"})
        self.assertTrue(all("record" in item["data"] and "table" in item["data"]["_ld"] for item in items))
        database_item = next(item for item in items if item["data"]["_ld"]["folder"] == "dbs")
        self.assertEqual(json.loads(database_item["data"]["record"]["binary_data"]),
                         {"$binary": "base64", "data": "AP8="})
        self.assertEqual(self.hashes(self.folder), original)

    def test_default_output_folders_are_excluded_on_repeat_import(self):
        source = self.write("people/1.csv", f"cpf;codigo\n{CPF};P\n")
        original = source.read_bytes()
        first = ingest_folder(self.folder, config=CONFIG, version="default-paths-v1")
        second = ingest_folder(self.folder, config=CONFIG)
        self.assertEqual(first["imported_rows"], 1)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["discovered_files"], 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(DatasetStore(self.folder / ".leitor" / "published", SECRET).ready())

    def test_unsearchable_tables_and_invalid_identifiers_remain_in_payload(self):
        self.write("notes/1.csv", "description;note\nhello;no identifiers\n")
        self.write("people/1.csv", f"cpf;codigo;nome\n{CPF};VALID;Ana\ninvalid-cpf;INVALID;Bia\n")
        report = self.ingest("raw-v1")
        self.assertEqual(report["imported_rows"], 3)
        notes = next(table for table in report["tables"] if table["logical_parent"] == "notes")
        self.assertEqual(notes["fields"], {})
        manifest = self.manifest("raw-v1")
        self.assertEqual(manifest["row_count"], 3)
        self.assertEqual(manifest["invalid_identifiers"]["cpf"], 1)
        store = self.store()
        self.assertEqual(len(store.search("cpf", CPF)["items"]), 1)
        invalid = store.search("code", "INVALID")["items"]
        self.assertEqual(invalid[0]["data"]["record"]["cpf"], "invalid-cpf")
        self.assertEqual(store.search("code", "no identifiers")["items"], [])
        note_payload = next(row for row in self.all_payloads("raw-v1") if row["_ld"]["folder"] == "notes")
        self.assertEqual(note_payload["record"], {"description": "hello", "note": "no identifiers"})


if __name__ == "__main__":
    unittest.main()
