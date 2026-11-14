from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from leitor_dados.builder import build_dataset, publish_version
from leitor_dados.query import (CursorExpired, DatasetStore, DatasetUnavailable,
                                QueryBusy, QueryTimeout, ResultTooLarge, open_snapshot)
from leitor_dados.__main__ import verify_dataset


CONFIG = {"format": "jsonl", "fields": {"cpf": "cpf", "email": "email", "phone": "phone", "code": "code"},
          "data_shards": 3, "lookup_shards": 5, "batch_size": 2}
SECRET = "test-only-cursor-secret-" + "x" * 32


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "data"
        self.source = self.base / "input.jsonl"
        self.rows = [{"cpf": "00000000000", "email": "Ana@EXAMPLE.invalid",
                      "phone": "+55 (11) 90000-0001", "code": "GROUP", "name": f"row-{n}"}
                     for n in range(7)]
        self.write_rows(self.rows)
        self.manifest = build_dataset([self.source], CONFIG, self.root, "v1", publish=True)
        self.store = DatasetStore(self.root, SECRET)

    def write_rows(self, rows):
        self.source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_routed_lookup_all_fields_and_payload(self):
        for field, value in [("cpf", "000.000.000-00"), ("email", "Ana@example.invalid"),
                             ("phone", "5511900000001"), ("code", "GROUP")]:
            with self.subTest(field=field):
                result = self.store.search(field, value, 100)
                self.assertEqual([item["data"] for item in result["items"]], self.rows)
                self.assertIsNone(result["next_cursor"])
        self.assertEqual(self.store.search("email", "ana@example.invalid")["items"], [])
        self.assertEqual(self.store.search("code", "' OR 1=1 --")["items"], [])

    def test_no_fanout_across_lookup_files(self):
        import leitor_dados.query as query
        opened = []
        actual = query.open_snapshot
        def track(path, deadline=None):
            opened.append(path.name)
            return actual(path, deadline)
        with patch.object(query, "open_snapshot", side_effect=track):
            self.store.search("code", "GROUP", 1)
        self.assertEqual(len([name for name in opened if name.startswith("lookup-")]), 1)
        self.assertEqual(len([name for name in opened if name.startswith("records-")]), 1)

    def test_cursor_remains_on_old_version_after_publication(self):
        first = self.store.search("code", "GROUP", 2)
        self.write_rows([{"code": "GROUP", "name": "new-version"}])
        build_dataset([self.source], CONFIG, self.root, "v2", publish=True)
        self.assertEqual(self.store.search("code", "GROUP")["version"], "v2")
        ids = [item["record_id"] for item in first["items"]]
        cursor = first["next_cursor"]
        while cursor:
            page = self.store.search("code", "GROUP", 2, cursor)
            self.assertEqual(page["version"], "v1")
            ids.extend(item["record_id"] for item in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(ids, list(range(1, 8)))

    def test_cursor_tampering_wrong_query_and_expiry(self):
        cursor = self.store.search("code", "GROUP", 2)["next_cursor"]
        with self.assertRaises(ValueError):
            self.store.search("code", "GROUP", 2, cursor + "x")
        with self.assertRaises(ValueError):
            self.store.search("code", "OTHER", 2, cursor)
        with patch("leitor_dados.query.time.time", return_value=time.time() + 90000):
            with self.assertRaises(CursorExpired):
                self.store.search("code", "GROUP", 2, cursor)

    def test_snapshot_connections_cannot_write_and_search_does_not_change_files(self):
        def hashes():
            return {p.name: hashlib.sha256(p.read_bytes()).digest()
                    for p in (self.root / "versions" / "v1").iterdir()}
        before = hashes()
        self.store.search("cpf", "00000000000")
        path = self.root / "versions" / "v1" / "records-0000.sqlite"
        with closing(open_snapshot(path)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM records")
        self.assertEqual(before, hashes())

    def test_busy_timeout_and_response_bounds(self):
        store = DatasetStore(self.root, SECRET, max_concurrent_queries=1)
        store.slots.acquire()
        try:
            with self.assertRaises(QueryBusy):
                store.search("code", "GROUP")
        finally:
            store.slots.release()
        self.assertEqual(len(store.search("code", "GROUP")["items"]), 7)
        with self.assertRaises(QueryTimeout):
            DatasetStore(self.root, SECRET, query_timeout_seconds=1e-12).search("code", "GROUP")
        with self.assertRaises(ResultTooLarge):
            DatasetStore(self.root, SECRET, max_payload_bytes=10).search("code", "GROUP")
        for kwargs in ({"query_timeout_seconds": float("nan")}, {"cursor_ttl_seconds": 0}):
            with self.assertRaises(ValueError):
                DatasetStore(self.root, SECRET, **kwargs)

    def test_manifest_validation_and_readiness_detect_missing_shard(self):
        self.assertTrue(self.store.ready())
        self.store.search("code", "GROUP")
        (self.root / "versions" / "v1" / "lookup-0000.sqlite").unlink()
        self.assertFalse(self.store.ready())
        with self.assertRaises(DatasetUnavailable):
            DatasetStore(self.root, SECRET).search("code", "GROUP")

    def test_invalid_metadata_and_path_are_rejected(self):
        for version in ("../escape", "/absolute", ".."):
            with self.assertRaises(ValueError):
                publish_version(self.root, version)
        path = self.root / "versions" / "v1" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["normalization"] = {"email": {"lowercase": "yes"}}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertFalse(DatasetStore(self.root, SECRET).ready())

    def test_offline_checksums_detect_modification(self):
        result = verify_dataset(self.root, "v1", checksums=True, full=True)
        self.assertEqual(result["status"], "ok")
        path = self.root / "versions" / "v1" / "lookup-0000.sqlite"
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "Checksum"):
            verify_dataset(self.root, "v1", checksums=True)


if __name__ == "__main__":
    unittest.main()
