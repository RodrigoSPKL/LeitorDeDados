from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import init_secrets


class InitSecretsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "credentials"

    def initialize_silently(self):
        with redirect_stdout(io.StringIO()):
            init_secrets.initialize(self.directory)

    def contents(self):
        return {
            name: (self.directory / name).read_bytes()
            for name in init_secrets.SECRET_NAMES
        }

    def ownership_os(self, **overrides):
        # Replace this module's OS view without changing pathlib's host platform.
        values = dict(vars(os))
        values.update(name="posix", fchown=Mock(), fchmod=Mock())
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_creation_and_repeated_setup_preserve_credentials_without_printing_them(self):
        output = io.StringIO()
        with redirect_stdout(output):
            init_secrets.initialize(self.directory)
            original = self.contents()
            init_secrets.initialize(self.directory)

        self.assertEqual(self.contents(), original)
        self.assertNotEqual(original["api_key"], original["cursor_secret"])
        for name, value in original.items():
            self.assertGreaterEqual(len(value.strip()), 32)
            self.assertIn(f"{name}: criado", output.getvalue())
            self.assertIn(f"{name}: existente preservado", output.getvalue())
            self.assertNotIn(value.decode().strip(), output.getvalue())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)

    def test_short_existing_file_is_rejected_without_overwriting_it(self):
        self.directory.mkdir()
        path = self.directory / "api_key"
        path.write_bytes(b"short")

        with self.assertRaisesRegex(ValueError, "api_key"):
            init_secrets.initialize(self.directory)

        self.assertEqual(path.read_bytes(), b"short")
        self.assertFalse((self.directory / "cursor_secret").exists())

    def test_existing_directory_instead_of_key_is_rejected(self):
        (self.directory / "api_key").mkdir(parents=True)
        with self.assertRaisesRegex((OSError, ValueError), "api_key"):
            init_secrets.initialize(self.directory)
        self.assertTrue((self.directory / "api_key").is_dir())

    def test_symlink_key_is_rejected_without_changing_its_target(self):
        self.directory.mkdir()
        target = Path(self.temporary.name) / "external-key"
        original = b"unit-test-external-key-" + b"z" * 32
        target.write_bytes(original)
        try:
            (self.directory / "api_key").symlink_to(target)
        except OSError as error:
            self.skipTest(f"Host does not permit creating symlinks: {error}")

        with self.assertRaisesRegex(ValueError, "api_key"):
            init_secrets.initialize(self.directory)

        self.assertEqual(target.read_bytes(), original)

    def test_ownership_setup_updates_both_files_without_rotating_credentials(self):
        self.initialize_silently()
        original = self.contents()
        operations = []
        host_os = self.ownership_os(
            fchown=Mock(side_effect=lambda fd, uid, gid: operations.append(("owner", uid, gid))),
            fchmod=Mock(side_effect=lambda fd, mode: operations.append(("mode", mode))),
        )

        with patch.object(init_secrets, "os", host_os), redirect_stdout(io.StringIO()):
            init_secrets.initialize(self.directory, uid=10001, gid=10001)

        self.assertEqual(self.contents(), original)
        self.assertEqual(operations, [("owner", 10001, 10001), ("mode", 0o600)] * 2)

    def test_denied_ownership_keeps_existing_credentials_and_identifies_failed_file(self):
        self.initialize_silently()
        original = self.contents()
        host_os = self.ownership_os(fchown=Mock(side_effect=PermissionError(1, "Operation not permitted")))
        output = io.StringIO()

        with patch.object(init_secrets, "os", host_os), redirect_stdout(output):
            with self.assertRaises(OSError) as caught:
                init_secrets.initialize(self.directory, uid=10001, gid=10001)

        self.assertEqual(self.contents(), original)
        self.assertIn("api_key", str(caught.exception))
        self.assertIn("UID/GID", str(caught.exception))
        host_os.fchmod.assert_not_called()
        for value in original.values():
            self.assertNotIn(value.decode().strip(), output.getvalue() + str(caught.exception))

    def test_failed_new_file_permissions_remove_only_the_new_credential(self):
        self.directory.mkdir()
        existing_cursor = b"unit-test-existing-cursor-" + b"q" * 32
        cursor_path = self.directory / "cursor_secret"
        cursor_path.write_bytes(existing_cursor)
        generated_key = "unit-test-generated-api-key-" + "r" * 32
        host_os = self.ownership_os(
            fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)),
            chmod=Mock(side_effect=PermissionError(1, "Operation not permitted")),
        )
        output = io.StringIO()

        with patch.object(init_secrets, "os", host_os), patch.object(
            init_secrets.secrets, "token_urlsafe", return_value=generated_key
        ), redirect_stdout(output):
            with self.assertRaises(OSError):
                init_secrets.initialize(self.directory)

        self.assertFalse((self.directory / "api_key").exists())
        self.assertEqual(cursor_path.read_bytes(), existing_cursor)
        self.assertNotIn(generated_key, output.getvalue())

    def test_filesystem_without_private_mode_is_rejected_before_generating_key(self):
        self.directory.mkdir()
        existing_cursor = b"unit-test-existing-cursor-" + b"s" * 32
        cursor_path = self.directory / "cursor_secret"
        cursor_path.write_bytes(existing_cursor)
        host_os = self.ownership_os(
            fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o755)),
        )

        with patch.object(init_secrets, "os", host_os), patch.object(
            init_secrets.secrets, "token_urlsafe"
        ) as generate, redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError) as caught:
                init_secrets.initialize(self.directory)

        generate.assert_not_called()
        self.assertFalse((self.directory / "api_key").exists())
        self.assertEqual(cursor_path.read_bytes(), existing_cursor)
        self.assertIn("api_key", str(caught.exception))
        self.assertIn("0600", str(caught.exception))

    def test_denied_private_mode_keeps_existing_credentials_and_identifies_failed_file(self):
        self.initialize_silently()
        original = self.contents()
        host_os = self.ownership_os(fchmod=Mock(side_effect=PermissionError(1, "Operation not permitted")))

        with patch.object(init_secrets, "os", host_os), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError) as caught:
                init_secrets.initialize(self.directory, uid=10001, gid=10001)

        self.assertEqual(self.contents(), original)
        self.assertIn("api_key", str(caught.exception))
        self.assertIn("0600", str(caught.exception))
        host_os.fchown.assert_called_once()

    def test_cli_reports_permission_failure_without_exposing_key_values(self):
        self.initialize_silently()
        original = self.contents()
        host_os = self.ownership_os(fchown=Mock(side_effect=PermissionError(1, "Operation not permitted")))
        output, errors = io.StringIO(), io.StringIO()
        arguments = ["init_secrets.py", "--directory", str(self.directory), "--uid", "10001", "--gid", "10001"]

        with patch.object(init_secrets, "os", host_os), patch.object(
            init_secrets.sys, "argv", arguments
        ), redirect_stdout(output), redirect_stderr(errors):
            status = init_secrets.main()

        self.assertEqual(status, 1)
        self.assertIn("api_key", errors.getvalue())
        self.assertEqual(self.contents(), original)
        for value in original.values():
            self.assertNotIn(value.decode().strip(), output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
