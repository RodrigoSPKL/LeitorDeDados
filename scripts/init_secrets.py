"""Create local credentials without replacing existing secrets or printing them."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import stat
import sys


SECRET_NAMES = ("api_key", "cursor_secret")


def initialize(directory: Path, uid: int | None = None, gid: int | None = None) -> None:
    if (uid is None) != (gid is None):
        raise ValueError("--uid e --gid devem ser fornecidos juntos")
    if uid is not None and (os.name != "posix" or uid < 0 or gid < 0):
        raise ValueError("ownership exige Linux/POSIX e UID/GID não negativos")

    directory = directory.absolute()
    if directory.is_symlink():
        raise ValueError("o diretório de segredos não pode ser link simbólico")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    for name in SECRET_NAMES:
        path = directory / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{name}: o caminho existente não é arquivo regular")
            if metadata.st_size < 32:
                raise ValueError(f"{name}: arquivo existente curto ou vazio; não foi alterado")
            created = False
        else:
            try:
                with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
                    if os.name == "posix" and stat.S_IMODE(os.fstat(handle.fileno()).st_mode) != 0o600:
                        raise ValueError(
                            f"{name}: o sistema de arquivos não aplicou o modo privado 0600. "
                            "Use uma pasta Linux com permissões POSIX, como ext4 ou XFS."
                        )
                    handle.write(secrets.token_urlsafe(32) + "\n")
                os.chmod(path, 0o600)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            created = True

        if uid is not None:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError(f"{name}: ownership exige arquivo regular")
                try:
                    os.fchown(descriptor, uid, gid)
                except OSError as exc:
                    raise OSError(
                        exc.errno,
                        f"{name}: não foi possível ajustar UID/GID para {uid}:{gid}. "
                        "Execute a preparação no host com sudo e use um sistema de arquivos "
                        "Linux com permissões POSIX, como ext4 ou XFS; exFAT não permite esse ajuste.",
                        str(path),
                    ) from exc
                try:
                    os.fchmod(descriptor, 0o600)
                except OSError as exc:
                    raise OSError(
                        exc.errno,
                        f"{name}: não foi possível aplicar o modo 0600. "
                        "Mantenha as credenciais em um sistema de arquivos Linux com permissões POSIX.",
                        str(path),
                    ) from exc
            finally:
                os.close(descriptor)
        print(f"{name}: {'criado' if created else 'existente preservado'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "secrets",
    )
    parser.add_argument("--uid", type=int, help="UID Linux do processo que lerá os segredos")
    parser.add_argument("--gid", type=int, help="GID Linux do processo que lerá os segredos")
    args = parser.parse_args()
    try:
        initialize(args.directory, args.uid, args.gid)
    except (OSError, ValueError) as exc:
        print(f"Falha ao preparar segredos: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
