"""Preview, quarantine or delete only temporary RAR extraction directories."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid


PARTIAL = re.compile(r"\.rar-pending-[a-z0-9_]{8}\Z")


class CleanupError(ValueError):
    pass


def _link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _check_lock(root: Path) -> None:
    for parent in (root, *root.parents):
        lock = parent / ".rar-extraction-lock"
        if lock.exists() or _link(lock):
            raise CleanupError("Existe uma reserva de extração ativa ou pendente. Confira se o extrator terminou antes da limpeza.")


def _checked(root: Path, path: Path) -> Path:
    path = path.absolute()
    if path == root or not path.is_relative_to(root) or not PARTIAL.fullmatch(path.name):
        raise CleanupError("Alvo não é uma pasta parcial permitida dentro da origem.")
    if any(parent.name.casefold() == "backup" for parent in path.relative_to(root).parents):
        raise CleanupError("Pastas dentro de backup são preservadas.")
    for current in (path, *path.parents):
        if _link(current):
            raise CleanupError("Links não são aceitos como alvo nem como caminho de limpeza.")
        if current == root:
            break
    resolved = path.resolve(strict=True)
    if resolved == root or not resolved.is_relative_to(root) or not resolved.is_dir():
        raise CleanupError("Alvo fora da origem ou não é diretório.")
    return resolved


def _raise_walk_error(error):
    raise error


def _bytes(folder: Path) -> int:
    total = 0
    for current, directories, files in os.walk(folder, followlinks=False, onerror=_raise_walk_error):
        if ".rar-extraction-lock" in directories or ".rar-extraction-lock" in files:
            raise CleanupError("Há uma reserva de extração dentro de uma pasta parcial; limpeza interrompida.")
        directories[:] = [name for name in directories if not _link(Path(current) / name)]
        for name in files:
            info = (Path(current) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _plan(root: Path) -> list[dict]:
    items = []
    for current, directories, _ in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        selected = []
        for name in sorted(directories):
            path = Path(current) / name
            if name == ".rar-extraction-lock":
                raise CleanupError("Há uma reserva de extração em uma subpasta; confira antes de limpar.")
            if name.casefold() == "backup" or _link(path):
                continue
            if PARTIAL.fullmatch(name):
                path = _checked(root, path)
                info = path.stat()
                items.append({"path": path.relative_to(root).as_posix(), "bytes": _bytes(path),
                              "device": info.st_dev, "inode": info.st_ino})
            else:
                selected.append(name)
        directories[:] = selected
    return items


def _write_manifest(path: Path, report: dict) -> None:
    temporary = path.with_name(".manifest-" + uuid.uuid4().hex + ".json")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def cleanup(folder: Path, mode: str = "preview") -> dict:
    if mode not in ("preview", "quarantine", "delete"):
        raise CleanupError("Modo de limpeza inválido.")
    folder = Path(folder).absolute()
    if _link(folder):
        raise CleanupError("Escolha uma pasta real, sem link simbólico.")
    root = folder.resolve(strict=True)
    if not root.is_dir() or root.parent == root or any(part.casefold() == "backup" for part in root.parts):
        raise CleanupError("Escolha a pasta dos dados, fora de backup e da raiz do sistema.")
    _check_lock(root)
    plan = _plan(root)
    report = {"mode": mode, "root": str(root), "candidates": plan,
              "total_bytes": sum(item["bytes"] for item in plan), "processed": [], "errors": []}
    if mode == "preview" or not plan:
        return report
    backup = root / "backup"
    if _link(backup):
        raise CleanupError("backup não pode ser um link.")
    backup.mkdir(exist_ok=True)
    run = backup / ("limpeza-parciais-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    run.mkdir()
    report["manifest"] = str(run / "manifest.json")
    _write_manifest(run / "manifest.json", report)
    for item in plan:
        try:
            _check_lock(root)
            source = _checked(root, root / item["path"])
            info = source.stat()
            if (info.st_dev, info.st_ino) != (item["device"], item["inode"]):
                raise CleanupError("A pasta foi substituída depois da prévia interna; alvo preservado.")
            if mode == "quarantine":
                destination = run / "pastas" / item["path"]
                if not destination.resolve().is_relative_to(run.resolve()):
                    raise CleanupError("Destino fora da área de limpeza.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or _link(destination):
                    raise CleanupError("O destino já existe; alvo preservado.")
                source.rename(destination)
                report["processed"].append({"source": item["path"], "destination": str(destination)})
            else:
                shutil.rmtree(source)
                report["processed"].append({"source": item["path"], "deleted": True})
        except (CleanupError, OSError) as error:
            report["errors"].append({"path": item["path"], "error": str(error)})
        _write_manifest(run / "manifest.json", report)
    return report


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--folder", type=Path, default=Path.cwd())
    action = cli.add_mutually_exclusive_group()
    action.add_argument("--quarantine", action="store_true", help="Move as parciais para backup, sem liberar espaço")
    action.add_argument("--delete", action="store_true", help="Apaga definitivamente somente as pastas parciais")
    args = cli.parse_args()
    mode = "delete" if args.delete else "quarantine" if args.quarantine else "preview"
    try:
        report = cleanup(args.folder, mode)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["errors"] else 0
    except (CleanupError, OSError) as error:
        print(f"Erro: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
