"""Behavioral checks for immutable streaming ingestion and publication."""

from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from leitor_dados.builder import BuildError, build_dataset, publish_version
from leitor_dados.normalization import data_shard, lookup_shard, normalize


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "dataset"
        self.config = {"format": "jsonl", "fields": {"cpf": "document", "email": "email", "code": "sku"},
                       "data_shards": 2, "lookup_shards": 3, "batch_size": 1}

    def write_jsonl(self, rows, name="input.jsonl"):
        path = self.base / name
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def read_payload(self, directory, record_id):
        path = directory / f"records-{data_shard(record_id, 2):04d}.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            return json.loads(connection.execute("SELECT payload FROM records WHERE record_id=?", (record_id,)).fetchone()[0])

    def test_normalization_preserves_significant_text(self):
        self.assertEqual(normalize("cpf", " 001.234.567-89 "), "00123456789")
        self.assertEqual(normalize("email", " User@EXAMPLE.COM "), "User@example.com")
        self.assertEqual(normalize("email", " User@EXAMPLE.COM ", {"email": {"lowercase": True}}), "user@example.com")
        self.assertEqual(normalize("phone", "+55 (11) 01234-5678"), "5511012345678")
        self.assertEqual(normalize("code", " 000AbC "), "000AbC")
        self.assertIsNone(normalize("cpf", " "))
        for kind, value in (("cpf", 12345678901), ("cpf", "１２３４５６７８９０１"),
                            ("phone", "call123"), ("email", "user@@example.com")):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    normalize(kind, value)

    def test_round_trip_route_hashes_and_duplicate_matches(self):
        rows = [{"document": "001.234.567-89", "email": "User@EXAMPLE.COM", "sku": "000A", "sale": {"total": "12.30"}},
                {"document": "00123456789", "email": None, "sku": "000A", "label": "ação"},
                {"label": "no identifiers"}]
        path = self.write_jsonl(rows)
        manifest = build_dataset([path], self.config, self.root, "v1", publish=True)
        directory = self.root / "versions" / "v1"
        self.assertEqual(manifest["row_count"], 3)
        self.assertEqual(json.loads((self.root / "current.json").read_text()), {"version": "v1"})
        for record_id, row in enumerate(rows, start=1):
            self.assertEqual(self.read_payload(directory, record_id), row)
        expected_shard = int.from_bytes(hashlib.blake2b(b"cpf\0" + b"00123456789", digest_size=8).digest(), "big") % 3
        self.assertEqual(lookup_shard("cpf", "00123456789", 3), expected_shard)
        lookup = directory / f"lookup-{expected_shard:04d}.sqlite"
        with closing(sqlite3.connect(lookup)) as connection:
            matches = connection.execute("SELECT record_id FROM lookup WHERE kind=? AND value=? ORDER BY record_id",
                                         ("cpf", "00123456789")).fetchall()
        self.assertEqual(matches, [(1,), (2,)])
        for info in manifest["files"]:
            self.assertEqual(info["sha256"], hashlib.sha256((directory / info["name"]).read_bytes()).hexdigest())
            self.assertEqual(info["bytes"], (directory / info["name"]).stat().st_size)

    def test_csv_streams_multiline_and_preserves_zeros(self):
        path = self.base / "input.csv"
        path.write_text('document;sku;comment\n00123456789;000A;"line one\nline two"\n', encoding="utf-8", newline="\n")
        config = dict(self.config, format="csv", csv_delimiter=";", fields={"cpf": "document", "code": "sku"})
        build_dataset([path], config, self.root, "csv")
        self.assertEqual(self.read_payload(self.root / "versions" / "csv", 1),
                         {"document": "00123456789", "sku": "000A", "comment": "line one\nline two"})
        self.assertFalse((self.root / "current.json").exists())

    def test_failed_import_cleans_stage_and_keeps_published_version(self):
        first = self.write_jsonl([{"document": "00123456789"}], "first.jsonl")
        build_dataset([first], self.config, self.root, "v1", publish=True)
        for bad_row in ({"document": 123456789}, {"document": "private-invalid-value"}):
            path = self.write_jsonl([{"document": "00123456789"}, bad_row], "bad.jsonl")
            with self.assertRaises(BuildError) as context:
                build_dataset([path], self.config, self.root, "bad", publish=True)
            self.assertNotIn("private-invalid-value", str(context.exception))
            self.assertNotIn("123456789", str(context.exception))
            self.assertEqual(json.loads((self.root / "current.json").read_text()), {"version": "v1"})
            self.assertFalse((self.root / "versions" / "bad").exists())
            self.assertEqual(list(self.root.glob(".build-*")), [])

    def test_existing_version_never_replaced_and_old_version_stays_pinned(self):
        old = self.write_jsonl([{"sku": "old"}], "old.jsonl")
        new = self.write_jsonl([{"sku": "new"}], "new.jsonl")
        build_dataset([old], self.config, self.root, "old", publish=True)
        pinned = self.root / "versions" / "old"
        with self.assertRaises(BuildError):
            build_dataset([new], self.config, self.root, "old", publish=True)
        build_dataset([new], self.config, self.root, "new", publish=True)
        self.assertEqual(self.read_payload(pinned, 1)["sku"], "old")
        self.assertEqual(json.loads((self.root / "current.json").read_text()), {"version": "new"})

    def test_repeated_input_path_is_rejected_but_distinct_files_are_preserved(self):
        first = self.write_jsonl([{"sku": "same"}], "first.jsonl")
        second = self.write_jsonl([{"sku": "same"}], "second.jsonl")
        alias = first.parent / "unused" / ".." / first.name
        with self.assertRaises(BuildError):
            build_dataset([first, alias], self.config, self.root, "duplicate", publish=True)
        self.assertFalse((self.root / "current.json").exists())
        manifest = build_dataset([first, second], self.config, self.root, "distinct")
        self.assertEqual(manifest["row_count"], 2)

    def test_mixed_csv_jsonl_sources_share_normalization_and_lookup(self):
        csv_path = self.base / "sales.txt"
        csv_path.write_text("cpf_number;note\n001.234.567-89;ação\n", encoding="cp1252", newline="\n")
        jsonl_path = self.write_jsonl([{"person_document": "00123456789", "name": "Usuário"}])
        overrides = [
            {"format": "csv", "csv_delimiter": ";", "encoding": "cp1252", "fields": {"cpf": "cpf_number"}},
            {"format": "jsonl", "fields": {"cpf": "person_document"}},
        ]
        manifest = build_dataset([csv_path, jsonl_path], self.config, self.root, "mixed",
                                 publish=True, source_configs=overrides)
        directory = self.root / "versions" / "mixed"
        self.assertEqual(manifest["row_count"], 2)
        self.assertEqual(self.read_payload(directory, 1), {"cpf_number": "001.234.567-89", "note": "ação"})
        self.assertEqual(self.read_payload(directory, 2), {"person_document": "00123456789", "name": "Usuário"})
        shard = lookup_shard("cpf", "00123456789", self.config["lookup_shards"])
        with closing(sqlite3.connect(directory / f"lookup-{shard:04d}.sqlite")) as connection:
            matches = connection.execute("SELECT record_id FROM lookup WHERE kind=? AND value=? ORDER BY record_id",
                                         ("cpf", "00123456789")).fetchall()
        self.assertEqual(matches, [(1,), (2,)])

    def test_source_overrides_reject_global_options_and_length_mismatch(self):
        path = self.write_jsonl([{"sku": "a"}])
        for source_configs in ([], [{"normalization": {}}], [{"data_shards": 1}], [None]):
            with self.subTest(source_configs=source_configs):
                with self.assertRaises(BuildError):
                    build_dataset([path], self.config, self.root, "invalid-source", publish=True,
                                  source_configs=source_configs)
        self.assertFalse((self.root / "current.json").exists())
        self.assertFalse((self.root / "versions" / "invalid-source").exists())

    def test_progress_reports_committed_counts_and_callback_failure_does_not_publish(self):
        first = self.write_jsonl([{"sku": "old"}], "first.jsonl")
        build_dataset([first], self.config, self.root, "old", publish=True)
        path = self.write_jsonl([{"sku": "a"}, {"sku": "b"}, {"sku": "c"}])
        counts = []
        config = dict(self.config, batch_size=2)
        build_dataset([path], config, self.root, "progress", progress=counts.append)
        self.assertEqual(counts, [2, 3])

        def fail_after_batch(count):
            self.assertEqual(count, 2)
            raise RuntimeError("callback failure")

        with self.assertRaisesRegex(RuntimeError, "callback failure"):
            build_dataset([path], config, self.root, "failed-progress", publish=True,
                          progress=fail_after_batch)
        self.assertEqual(json.loads((self.root / "current.json").read_text()), {"version": "old"})
        self.assertFalse((self.root / "versions" / "failed-progress").exists())
        self.assertEqual(list(self.root.glob(".build-*")), [])

    def test_malformed_inputs_and_oversized_payload_are_not_published(self):
        cases = [b'{"sku":"a","sku":"b"}\n', b'{"sku":NaN}\n', b'not json\n', b'[]\n', b'\xff\n']
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                path = self.base / "bad.jsonl"
                path.write_bytes(content)
                with self.assertRaises(BuildError):
                    build_dataset([path], self.config, self.root, f"bad{index}", publish=True)
        path = self.write_jsonl([{"sku": "long", "text": "x" * 300}])
        with patch("leitor_dados.builder.MAX_ROW_BYTES", 100):
            with self.assertRaises(BuildError):
                build_dataset([path], self.config, self.root, "large", publish=True)
        self.assertFalse((self.root / "current.json").exists())
        self.assertEqual(list(self.root.glob(".build-*")), [])

    def test_publishing_rejects_missing_traversal_and_tampered_file(self):
        with self.assertRaises(BuildError):
            publish_version(self.root, "../escape")
        with self.assertRaises(BuildError):
            publish_version(self.root, "missing")
        path = self.write_jsonl([{"sku": "a"}])
        manifest = build_dataset([path], self.config, self.root, "valid")
        shard = self.root / "versions" / "valid" / manifest["files"][0]["name"]
        with shard.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(BuildError):
            publish_version(self.root, "valid")
        self.assertFalse((self.root / "current.json").exists())


if __name__ == "__main__":
    unittest.main()
