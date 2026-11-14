"""Exercise Linux orchestration with real Bash and fake external commands."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash")


@unittest.skipUnless(BASH, "Bash is required for Linux orchestration checks")
class LinuxScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="leitor shell ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.base_shell = subprocess.run(
            [BASH, "-c", "pwd -P"], cwd=self.base,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        self.project = self.base / "project with spaces"
        (self.project / "scripts").mkdir(parents=True)
        (self.project / "config").mkdir()
        for relative in ("processar-pasta.sh", "scripts/linux-start.sh"):
            destination = self.project / relative
            destination.write_text(
                (PROJECT_ROOT / relative).read_text(encoding="utf-8"),
                encoding="utf-8", newline="\n",
            )
        self.inputs = self.base / "source with spaces"
        self.inputs.mkdir()
        (self.inputs / "sample.csv").write_bytes(b"code\nDEMO-001\n")
        self.bin = self.base / "mockbin"
        self.bin.mkdir()
        self.log = self.base / "commands.log"
        self.env = os.environ.copy()
        # Git Bash supplies its POSIX utilities; no real Docker/Python/sudo runs.
        prefix = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.env.update(
            PATH=prefix,
            COMMAND_LOG=self.shell_path(self.log),
            FIXTURE_PROJECT=self.shell_path(self.project),
            SECURITY_OPTIONS='["name=seccomp,profile=builtin"]',
        )
        for name in ("DATA_PATH", "WORK_PATH", "BASH_ENV", "ENV"):
            self.env.pop(name, None)
        logger = (
            'printf "%s\\0" "${0##*/}" "$@" >> "$COMMAND_LOG"\n'
            'printf "\\n" >> "$COMMAND_LOG"\n'
        )
        self.write_command(
            "docker", logger
            + 'if [[ "${1:-}" == info ]]; then printf "%s\\n" "$SECURITY_OPTIONS"; fi\n',
        )
        self.write_command(
            "python3", logger
            + '[[ -d "$FIXTURE_PROJECT/.leitor" ]] || exit 87\n',
        )
        self.write_command("sudo", logger + 'exec "$@"\n')

    def write_command(self, name: str, contents: str):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + contents,
                        encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def shell_path(self, path: Path) -> str:
        # Git Bash exposes the native Windows temporary directory as /tmp.
        return f"{self.base_shell}/{path.relative_to(self.base).as_posix()}"

    def run_script(self, relative: str, *, cwd: Path | None = None,
                   extra_env: dict[str, str] | None = None):
        env = self.env | (extra_env or {})
        # Windows passes PATH through MSYS conversion unless set inside Bash.
        if os.name == "nt":
            command = [BASH, "-c",
                       'export PATH="$(/usr/bin/cygpath -u "$MOCK_BIN"):/usr/bin:/bin"; exec bash "$1"',
                       "fixture", (self.project / relative).as_posix()]
            env["MOCK_BIN"] = self.bin.as_posix()
            env["PATH"] = os.environ.get("PATH", "")
        else:
            command = [BASH, str(self.project / relative)]
        return subprocess.run(command, cwd=cwd or self.project, env=env,
                              capture_output=True, text=True, timeout=30)

    def commands(self):
        if not self.log.exists():
            return []
        return [line.rstrip(b"\0").decode("utf-8").split("\0")
                for line in self.log.read_bytes().splitlines()]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @staticmethod
    def mount_args(command: list[str]):
        return [command[index + 1] for index, value in enumerate(command)
                if value == "--mount"]

    def test_startup_prepares_secrets_on_host_and_precreates_workdir(self):
        result = self.run_script("scripts/linux-start.sh")
        self.assert_success(result)
        for name in ("data", "inputs", "secrets", ".leitor"):
            self.assertTrue((self.project / name).is_dir(), name)
        commands = self.commands()
        preparation = [command for command in commands if command[0] == "python3"]
        self.assertEqual(len(preparation), 1)
        self.assertTrue(preparation[0][1].endswith("/scripts/init_secrets.py"))
        self.assertEqual(preparation[0][-4:], ["--uid", "10001", "--gid", "10001"])
        self.assertIn("--directory", preparation[0])
        self.assertFalse(any("--entrypoint" in command for command in commands))
        self.assertTrue(any(command[:4] == ["docker", "compose", "up", "-d"]
                            for command in commands))

    def test_unsupported_user_namespaces_fail_before_host_mutations(self):
        for option in ("name=rootless", "name=userns"):
            with self.subTest(option=option):
                if self.log.exists():
                    self.log.unlink()
                result = self.run_script(
                    "scripts/linux-start.sh",
                    extra_env={"SECURITY_OPTIONS": f'["{option}"]'},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rootful", result.stderr)
                for name in ("data", "inputs", "secrets", ".leitor"):
                    self.assertFalse((self.project / name).exists(), name)
                self.assertEqual([command[0] for command in self.commands()],
                                 ["docker", "docker"])

    def test_startup_existing_external_snapshot_does_not_build_demo(self):
        destination = self.base / "published data"
        destination.mkdir()
        current = destination / "current.json"
        current.write_bytes(b'{"version":"existing"}\n')
        result = self.run_script(
            "scripts/linux-start.sh",
            extra_env={"DATA_PATH": self.shell_path(destination)},
        )
        self.assert_success(result)
        self.assertEqual(current.read_bytes(), b'{"version":"existing"}\n')
        self.assertFalse((self.project / "data").exists())
        self.assertFalse(any("demo-v1" in command for command in self.commands()))

    def test_importer_default_destination_is_in_caller_folder(self):
        original = (self.inputs / "sample.csv").read_bytes()
        result = self.run_script("processar-pasta.sh", cwd=self.inputs)
        self.assert_success(result)
        self.assertTrue((self.inputs / ".leitor" / "published").is_dir())
        self.assertTrue((self.inputs / ".leitor" / "tmp").is_dir())
        self.assertFalse((self.project / ".leitor").exists())
        run = next(command for command in self.commands() if command[:2] == ["docker", "run"])
        mounts = self.mount_args(run)
        self.assertTrue(any(mount.endswith("/source with spaces,dst=/scan,readonly")
                            for mount in mounts), mounts)
        self.assertTrue(any(mount.endswith("/source with spaces/.leitor,dst=/work")
                            for mount in mounts), mounts)
        self.assertIn("/scan/.leitor", run)
        self.assertEqual((self.inputs / "sample.csv").read_bytes(), original)

    def test_importer_external_work_path_leaves_source_without_derived_files(self):
        destination = self.base / "native filesystem" / "workspace"
        result = self.run_script(
            "processar-pasta.sh", cwd=self.inputs,
            extra_env={"WORK_PATH": self.shell_path(destination)},
        )
        self.assert_success(result)
        self.assertTrue((destination / "published").is_dir())
        self.assertTrue((destination / "tmp").is_dir())
        self.assertFalse((self.inputs / ".leitor").exists())
        self.assertEqual([path.name for path in self.inputs.iterdir()], ["sample.csv"])
        run = next(command for command in self.commands() if command[:2] == ["docker", "run"])
        mounts = self.mount_args(run)
        self.assertTrue(any(mount.endswith("/native filesystem/workspace,dst=/work")
                            for mount in mounts), mounts)
        self.assertTrue(any(mount.endswith("/native filesystem/workspace/published,dst=/data")
                            for mount in mounts), mounts)
        self.assertTrue(any(mount.endswith("/source with spaces,dst=/scan,readonly")
                            for mount in mounts), mounts)

    def test_importer_custom_internal_work_path_is_excluded_from_inputs(self):
        destination = self.inputs / "derived files" / "workspace"
        result = self.run_script(
            "processar-pasta.sh", cwd=self.inputs,
            extra_env={"WORK_PATH": self.shell_path(destination)},
        )
        self.assert_success(result)
        self.assertTrue((destination / "published").is_dir())
        run = next(command for command in self.commands() if command[:2] == ["docker", "run"])
        exclusions = [run[index + 1] for index, value in enumerate(run)
                      if value == "--exclude"]
        self.assertIn("/scan/.leitor", exclusions)
        self.assertIn("/scan/derived files/workspace", exclusions)

    def test_importer_rejects_source_and_ancestor_work_paths_before_writing(self):
        for destination in (self.shell_path(self.inputs),
                            self.shell_path(self.inputs / ".."), "/"):
            with self.subTest(destination=destination):
                result = self.run_script(
                    "processar-pasta.sh", cwd=self.inputs,
                    extra_env={"WORK_PATH": destination},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("WORK_PATH", result.stderr)
                self.assertEqual(self.commands(), [])
                self.assertEqual([path.name for path in self.inputs.iterdir()], ["sample.csv"])
                for name in ("published", "tmp"):
                    self.assertFalse((self.base / name).exists())


if __name__ == "__main__":
    unittest.main()
