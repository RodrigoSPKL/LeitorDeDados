"""Behavioral checks for bounded, safe discovery of archived input files."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import binascii
from pathlib import Path
import stat
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from leitor_dados.archives import (
    ArchiveError,
    ExtractionLimitError,
    ExtractionLimits,
    UnsafeArchiveError,
    discover_files,
)


def zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def stored_rar_bytes(name: bytes, content: bytes, extra_flags: int = 0) -> bytes:
    """A tiny, generated RAR3 fixture using its uncompressed storage method."""
    def header(kind: int, flags: int, body: bytes) -> bytes:
        data = struct.pack("<BHH", kind, flags, len(body) + 7) + body
        return struct.pack("<H", binascii.crc32(data) & 0xFFFF) + data
    file_data = struct.pack("<LLBLLBBHL", len(content), len(content), 3,
                            binascii.crc32(content), 0x00210000, 20, 0x30,
                            len(name), stat.S_IFREG | 0o644) + name
    return (b"Rar!\x1a\x07\x00" + header(0x73, 0, bytes(6))
            + header(0x74, 0x8000 | extra_flags, file_data) + content + header(0x7B, 0, b""))


def rar5_header(body: bytes) -> bytes:
    def vint(value: int) -> bytes:
        result = bytearray()
        while value >= 128:
            result.append((value & 127) | 128)
            value >>= 7
        result.append(value)
        return bytes(result)
    data = vint(len(body)) + body
    return struct.pack("<I", binascii.crc32(data)) + data


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "input"
        self.root.mkdir()
        self.workspace = self.base / "workspace"

    def write(self, name: str, content: bytes = b"code\nDEMO-001\n") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def tar(self, name: str, members: list[tuple[str, bytes]], mode: str = "w:gz") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, mode) as archive:
            for member_name, content in members:
                info = tarfile.TarInfo(member_name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return path

    def discover(self, limits: ExtractionLimits | None = None):
        return discover_files(self.root, self.workspace, limits)

    def test_direct_inputs_supported_extensions_and_ignored_directories(self):
        expected = [
            self.write("01.csv"), self.write("folder/02.tsv"),
            self.write("folder/03.txt"), self.write("folder/04.db", b"placeholder"),
            self.write("folder/05.sqlite"), self.write("folder/06.sqlite3"),
            self.write("folder/07.jsonl", b'{"code":"A"}\n'),
            self.write("folder/08.ndjson", b'{"code":"B"}\n'),
        ]
        self.write("ignored.pdf")
        for directory in (".venv", ".git", "__pycache__"):
            self.write(f"{directory}/private.csv")
        for directory in ("data", "secrets", "dist", ".leitor"):
            expected.append(self.write(f"{directory}/legitimate.csv"))
        found = self.discover()
        self.assertEqual({entry.path.resolve() for entry in found}, {path.resolve() for path in expected})
        self.assertEqual({entry.logical_parent for entry in found}, {"", "folder", "data", "secrets", "dist", ".leitor"})
        self.assertEqual([entry.source for entry in found], sorted(entry.source for entry in found))
        self.assertIn("folder/07.jsonl", [entry.source for entry in found])

    def test_nested_zip_preserves_original_directories_but_omits_archive_wrappers(self):
        nested = zip_bytes([("b/2.csv", b"code\nB\n")])
        original = self.write("dados/a.zip", zip_bytes([
            ("1.csv", b"code\nA\n"),
            ("pacote/inner.zip", nested),
            ("ignored.bin", b"not a supported source"),
        ]))
        before = hashlib.sha256(original.read_bytes()).digest()
        found = self.discover()
        by_source = {entry.source: entry for entry in found}
        self.assertEqual(set(by_source), {"dados/a.zip!1.csv", "dados/a.zip!pacote/inner.zip!b/2.csv"})
        self.assertEqual(by_source["dados/a.zip!1.csv"].logical_parent, "dados")
        self.assertEqual(by_source["dados/a.zip!pacote/inner.zip!b/2.csv"].logical_parent, "dados/pacote/b")
        self.assertEqual(by_source["dados/a.zip!pacote/inner.zip!b/2.csv"].path.read_bytes(), b"code\nB\n")
        self.assertTrue(all(entry.path.resolve().is_relative_to(self.workspace.resolve()) for entry in found))
        self.assertEqual(hashlib.sha256(original.read_bytes()).digest(), before)

    def test_tar_compressions_and_nested_zip_have_the_same_logical_folder_semantics(self):
        for extension, mode in (("tar.gz", "w:gz"), ("tar.bz2", "w:bz2"), ("tar.xz", "w:xz")):
            with self.subTest(extension=extension):
                self.tar(f"{extension}/archive.{extension}", [
                    ("files/one.tsv", b"code\tname\n1\tA\n"),
                    ("files/inside.zip", zip_bytes([("two.jsonl", b'{"code":"2"}\n')])),
                ], mode)
        found = self.discover()
        self.assertEqual(len(found), 6)
        for entry in found:
            self.assertEqual(entry.logical_parent, entry.source.split("/archive.", 1)[0] + "/files")
            self.assertIn("!files/", entry.source)

    def test_workspace_inside_input_tree_is_not_discovered(self):
        self.write("source.csv")
        self.workspace = self.root / "generated-cache"
        self.workspace.mkdir()
        (self.workspace / "should-not-import.csv").write_text("private\n", encoding="utf-8")
        first = self.discover()
        second = self.discover()
        self.assertEqual([item.source for item in first], ["source.csv"])
        self.assertEqual([item.source for item in second], ["source.csv"])

    def test_explicit_exclusion_is_precise_and_cannot_exclude_the_root(self):
        self.write("data/legitimate.csv")
        self.write("generated/private.csv")
        found = discover_files(self.root, self.workspace, excluded_paths=[Path("generated")])
        self.assertEqual([entry.source for entry in found], ["data/legitimate.csv"])
        with self.assertRaises(UnsafeArchiveError):
            discover_files(self.root, self.workspace, excluded_paths=[self.root])

    def test_tar_dot_prefixes_are_safe_and_preserve_logical_folders(self):
        self.tar("normal.tar", [("./folder/one.csv", b"code\nA\n")], mode="w")
        found = self.discover()
        self.assertEqual([(entry.logical_parent, entry.source) for entry in found], [("folder", "normal.tar!folder/one.csv")])

    def test_oversized_tar_pax_metadata_is_rejected_before_payload(self):
        with tarfile.open(self.root / "metadata.tar", "w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("one.csv")
            info.pax_headers = {"comment": "x" * (1024 * 1024 + 1)}
            archive.addfile(info)
        with self.assertRaises(ExtractionLimitError):
            self.discover()

    def test_gnu_and_pax_sparse_metadata_are_rejected_before_expansion(self):
        from leitor_dados.archives import UnsupportedArchive
        for variant in ("gnu", "pax"):
            with self.subTest(variant=variant):
                path = self.root / "sparse.tar"
                with tarfile.open(path, "w", format=tarfile.GNU_FORMAT if variant == "gnu" else tarfile.PAX_FORMAT) as archive:
                    info = tarfile.TarInfo("one.csv")
                    if variant == "gnu":
                        info.type = tarfile.GNUTYPE_SPARSE
                    else:
                        info.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": "0"}
                    archive.addfile(info)
                with self.assertRaises(UnsupportedArchive):
                    self.discover()
                path.unlink()

    def test_rar_without_an_approved_backend_has_actionable_error(self):
        from leitor_dados.archives import UnsupportedArchive
        self.write("source.rar", b"Rar!\x1a\x07\x00")
        with patch("leitor_dados.archives.shutil.which", return_value=None):
            with self.assertRaisesRegex(UnsupportedArchive, "rarfile|unar|unrar"):
                self.discover()

    def test_rar_stored_payload_uses_real_parser_and_stream_reader(self):
        self.write("folder/data.rar", stored_rar_bytes(b"b/one.csv", b"code\nA\n"))
        # Stored RAR data does not invoke the external decoder; only its availability is simulated.
        with patch("leitor_dados.archives.shutil.which", return_value="approved-backend"):
            found = self.discover()
        self.assertEqual([(entry.logical_parent, entry.source) for entry in found], [("folder/b", "folder/data.rar!b/one.csv")])
        self.assertEqual(found[0].path.read_bytes(), b"code\nA\n")

    def test_rar_invalid_declared_utf8_name_is_not_silently_replaced(self):
        self.write("invalid.rar", stored_rar_bytes(b"\xff.csv", b"code\nA\n", extra_flags=0x200))
        with patch("leitor_dados.archives.shutil.which", return_value="approved-backend"):
            with self.assertRaises(UnsafeArchiveError):
                self.discover()

    def test_rar5_overlong_header_varint_is_bounded_before_library_parser(self):
        self.write("metadata.rar", b"Rar!\x1a\x07\x01\x00" + bytes(4) + b"\xff" * 64)
        with patch("leitor_dados.archives.shutil.which", return_value="approved-backend"):
            with self.assertRaises(ExtractionLimitError):
                self.discover()

    def test_rar5_stored_payload_uses_real_parser_and_stream_reader(self):
        name, content = b"b/one.csv", b"code\nA\n"
        # Small values fit in one-byte varints; file flags include its CRC32.
        body = (bytes((2, 2, len(content), 4, len(content)))
                + b"\xa4\x83\x02"  # POSIX regular-file mode 0100644
                + struct.pack("<I", binascii.crc32(content))
                + bytes((0, 1, len(name))) + name)
        archive = (b"Rar!\x1a\x07\x01\x00" + rar5_header(bytes((1, 0, 0)))
                   + rar5_header(body) + content + rar5_header(bytes((5, 0, 0))))
        self.write("modern.rar", archive)
        with patch("leitor_dados.archives.shutil.which", return_value="approved-backend"):
            found = self.discover()
        self.assertEqual([entry.source for entry in found], ["modern.rar!b/one.csv"])
        self.assertEqual(found[0].path.read_bytes(), content)

    def test_rar5_internal_overlong_varint_is_bounded_before_library_parser(self):
        body = bytes((2, 2, 0, 0)) + b"\xff" * 64
        self.write("internal.rar", b"Rar!\x1a\x07\x01\x00" + rar5_header(body))
        with patch("leitor_dados.archives.shutil.which", return_value="approved-backend"):
            with self.assertRaises(ExtractionLimitError):
                self.discover()

    def test_zip64_directory_is_supported_without_large_fixture(self):
        content = zip_bytes([("one.csv", b"code\nA\n")])
        position = len(content) - 22
        end = list(struct.unpack("<4s4H2IH", content[position:]))
        count, size, start = end[4], end[5], end[6]
        record = struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, start)
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, position, 1)
        end[3] = end[4] = 0xFFFF
        end[5] = end[6] = 0xFFFFFFFF
        self.write("wide.zip", content[:position] + record + locator + struct.pack("<4s4H2IH", *end))
        self.assertEqual([entry.source for entry in self.discover()], ["wide.zip!one.csv"])

    def test_repeat_scan_reuses_completed_extraction_and_preserves_source(self):
        original = self.write("a.zip", zip_bytes([("one.csv", b"code\nA\n")]))
        original_bytes = original.read_bytes()
        first = self.discover()
        first_metadata = [(entry.path, entry.path.stat().st_mtime_ns) for entry in first]
        with patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("completed archive re-extracted")):
            second = self.discover()
        self.assertEqual(first_metadata, [(entry.path, entry.path.stat().st_mtime_ns) for entry in second])
        self.assertEqual(original.read_bytes(), original_bytes)

    def test_literal_source_delimiters_cannot_collide_with_archive_layers(self):
        self.write("a.zip", zip_bytes([("b.csv", b"code\nARCHIVED\n")]))
        self.write("a.zip!b.csv", b"code\nDIRECT\n")
        self.write("a.zip%21b.csv", b"code\nPERCENT\n")
        found = self.discover()
        self.assertEqual({entry.source for entry in found}, {"a.zip!b.csv", "a.zip%21b.csv", "a.zip%2521b.csv"})

    def test_archive_changed_during_hash_is_not_published_in_cache(self):
        from leitor_dados import archives
        path = self.write("changing.zip", zip_bytes([("one.csv", b"code\nA\n")]))
        original_hash = archives._hash_file
        def changed_during_hash(source):
            digest = original_hash(source)
            with source.open("ab") as stream:
                stream.write(b"external change")
            return digest
        with patch("leitor_dados.archives._hash_file", side_effect=changed_during_hash):
            with self.assertRaisesRegex(ArchiveError, "mudou"):
                self.discover()
        self.assertTrue(path.is_file())
        self.assertFalse(list(self.workspace.glob("expanded/*/complete.json")))

    def test_zip_path_traversal_absolute_and_windows_names_are_rejected(self):
        for number, name in enumerate(("../outside.csv", "/outside.csv", "C:/outside.csv", "C:\\outside.csv", "..\\outside.csv")):
            with self.subTest(name=name):
                path = self.write("unsafe.zip", zip_bytes([(name, b"secret\n")]))
                with self.assertRaises(UnsafeArchiveError):
                    self.discover()
                self.assertTrue(path.is_file())
                self.assertFalse((self.base / "outside.csv").exists())
                path.unlink()

    def test_zip_symlink_is_rejected(self):
        info = zipfile.ZipInfo("link.csv")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        path = self.root / "links.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(info, "../outside.csv")
        with self.assertRaises(UnsafeArchiveError):
            self.discover()

    def test_tar_links_devices_and_pipes_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                path = self.root / "unsafe.tar"
                with tarfile.open(path, "w") as archive:
                    info = tarfile.TarInfo("unsafe.csv")
                    info.type = kind
                    info.linkname = "../outside.csv"
                    archive.addfile(info)
                with self.assertRaises(UnsafeArchiveError):
                    self.discover()
                path.unlink()

    def test_case_colliding_archive_members_are_rejected(self):
        self.write("duplicates.zip", zip_bytes([("same.csv", b"one\n"), ("SAME.CSV", b"two\n")]))
        with self.assertRaises(UnsafeArchiveError):
            self.discover()

    def test_single_member_declared_size_is_bounded(self):
        self.write("large.zip", zip_bytes([("one.csv", b"x" * 32)]))
        with self.assertRaises(ExtractionLimitError):
            self.discover(replace(ExtractionLimits(), max_single_file_bytes=16))

    def test_total_expansion_and_member_count_are_bounded(self):
        self.write("many.zip", zip_bytes([("one.csv", b"x" * 8), ("two.csv", b"y" * 8), ("three.csv", b"z" * 8)]))
        for limits in (replace(ExtractionLimits(), max_expanded_bytes=12), replace(ExtractionLimits(), max_members=2)):
            with self.subTest(limits=limits):
                with self.assertRaises(ExtractionLimitError):
                    self.discover(limits)

    def test_limits_apply_across_multiple_archives(self):
        self.write("one.zip", zip_bytes([("one.csv", b"x" * 8)]))
        self.write("two.zip", zip_bytes([("two.csv", b"y" * 8)]))
        with self.assertRaises(ExtractionLimitError):
            self.discover(replace(ExtractionLimits(), max_expanded_bytes=12))

    def test_actual_stream_bytes_are_bounded_even_if_header_understates_size(self):
        self.write("understated.zip", zip_bytes([("one.csv", b"x")]))
        with patch.object(zipfile.ZipFile, "open", return_value=io.BytesIO(b"x" * 32)):
            with self.assertRaises(ExtractionLimitError):
                self.discover(replace(ExtractionLimits(), max_single_file_bytes=16))

    def test_cached_extractions_do_not_bypass_new_smaller_limits(self):
        self.write("cache.zip", zip_bytes([("one.csv", b"x" * 32)]))
        self.assertEqual(len(self.discover()), 1)
        with self.assertRaises(ExtractionLimitError):
            self.discover(replace(ExtractionLimits(), max_expanded_bytes=16))

    def test_nested_archive_depth_is_bounded(self):
        self.write("outer.zip", zip_bytes([("inner.zip", zip_bytes([("one.csv", b"code\nA\n")]))]))
        with self.assertRaises(ExtractionLimitError):
            self.discover(replace(ExtractionLimits(), max_depth=1))

    def test_encrypted_zip_has_a_controlled_failure(self):
        content = bytearray(zip_bytes([("one.csv", b"code\nA\n")]))
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            position = content.index(signature) + offset
            flags = struct.unpack_from("<H", content, position)[0]
            struct.pack_into("<H", content, position, flags | 1)
        self.write("encrypted.zip", bytes(content))
        with self.assertRaises(ArchiveError):
            self.discover()

    def test_invalid_utf8_zip_filename_has_a_controlled_failure(self):
        content = bytearray(zip_bytes([("a.csv", b"code\nA\n")]))
        for signature, flag_offset, name_offset in ((b"PK\x03\x04", 6, 30), (b"PK\x01\x02", 8, 46)):
            position = content.index(signature)
            flags = struct.unpack_from("<H", content, position + flag_offset)[0]
            struct.pack_into("<H", content, position + flag_offset, flags | 0x800)
            content[position + name_offset] = 0xFF
        self.write("invalid-name.zip", bytes(content))
        with self.assertRaises(ArchiveError):
            self.discover()

    def test_input_symlink_never_imports_target_outside_root(self):
        outside = self.base / "outside.csv"
        outside.write_text("private\n", encoding="utf-8")
        link = self.root / "link.csv"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("host does not allow creating symbolic links")
        try:
            found = self.discover()
        except UnsafeArchiveError:
            found = []
        self.assertFalse(any(entry.path.resolve() == outside.resolve() for entry in found))
        self.assertEqual(outside.read_text(encoding="utf-8"), "private\n")


if __name__ == "__main__":
    unittest.main()
