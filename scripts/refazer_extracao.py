"""Reset one explicitly selected data folder, preserving backup, then rebuild RAR outputs."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

try:
    from . import extrair_rars as rar
except ImportError:
    import extrair_rars as rar


SESSION = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}\Z")


def _walk_error(error):
    raise error


def _link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _identity(path: Path) -> tuple:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _fingerprint(volumes: list[Path]) -> tuple:
    result = []
    for volume in volumes:
        before = _identity(volume)
        digest = hashlib.sha256()
        with volume.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
                digest.update(chunk)
        if _identity(volume) != before:
            raise rar.ExtractionError("Um volume mudou durante a conferência do backup.")
        result.append((before[2], digest.hexdigest()))
    return tuple(result)


def _logical(path: Path, backup: Path) -> Path:
    relative = path.relative_to(backup)
    if SESSION.fullmatch(relative.parts[0]):
        relative = Path(*relative.parts[1:])
    return relative


def _plan(backup: Path) -> tuple[list[dict], list[dict]]:
    candidates = []
    for current, directories, files in os.walk(backup, followlinks=False, onerror=_walk_error):
        directories[:] = sorted(name for name in directories
                                if not _link(Path(current) / name)
                                and not name.startswith(("limpeza-parciais-", "reset-extracao-", ".rar-pending-")))
        for name in files:
            path = Path(current) / name
            if path.suffix.casefold() == ".rar" and not _link(path):
                candidates.append(path)
    candidates.sort(key=lambda path: (len(_logical(path, backup).parts), _logical(path, backup).as_posix(), str(path)))
    groups, skipped, seen_volumes, fingerprints, outputs = [], [], set(), set(), set()
    covered_outputs = []
    for archive in candidates:
        if archive in seen_volumes:
            continue
        logical = _logical(archive, backup)
        if any(logical.is_relative_to(output) for output in covered_outputs):
            skipped.append({"archive": str(archive.relative_to(backup)), "reason": "já contido em um RAR pai"})
            continue
        volumes, expanded = rar._inspect(archive)
        if any(not path.is_relative_to(backup) for path in volumes):
            raise rar.ExtractionError("Um volume está fora de backup.")
        seen_volumes.update(volumes)
        first = _logical(volumes[0], backup)
        output = first.parent / (first.stem + "_extraido")
        if output.parts[0].casefold() in ("backup", ".rar-extraction-lock"):
            raise rar.ExtractionError("Um arquivo tenta usar o nome reservado de backup como saída.")
        covered_outputs.append(output)
        print(f"Conferindo conjunto do backup: {first}", flush=True)
        fingerprint = _fingerprint(volumes)
        if fingerprint in fingerprints:
            skipped.append({"archive": str(archive.relative_to(backup)), "reason": "conjunto compactado idêntico (SHA-256)"})
            continue
        canonical = output.as_posix().casefold()
        if canonical in outputs:
            raise rar.ExtractionError(f"Backups diferentes geram a mesma pasta {output}; escolha a versão correta antes do reset.")
        fingerprints.add(fingerprint)
        outputs.add(canonical)
        groups.append({"archive": volumes[0], "volumes": volumes, "expanded": expanded,
                       "output": output, "fingerprint": fingerprint,
                       "identities": {path: _identity(path) for path in volumes}})
    if not groups:
        raise rar.ExtractionError("Backup não contém conjuntos RAR utilizáveis. Nada será apagado.")
    return groups, skipped


def _write_report(run: Path, report: dict) -> None:
    temporary = run / (".report-" + uuid.uuid4().hex + ".json")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(run / "report.json")
    finally:
        temporary.unlink(missing_ok=True)


def _test(archive: Path, log: Path) -> None:
    with log.open("x+b") as stream:
        result = subprocess.run(["unrar", "t", "-cfg-", "-p-", str(archive)],
                                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                env={key: value for key, value in os.environ.items() if key != "RAR"})
        if result.returncode:
            raise rar.ExtractionError(f"Teste completo falhou; nada será apagado. Log: {log}\n{rar._diagnostic_tail(stream)}")


def _preflight_tree(root: Path, backup: Path) -> None:
    # Refuse nested mount points before any removal. Links are unlinked, not followed.
    device = root.stat().st_dev
    for current, directories, _ in os.walk(root, followlinks=False, onerror=_walk_error):
        directories[:] = [name for name in directories if Path(current) / name != backup
                           and not _link(Path(current) / name)]
        for name in directories:
            path = Path(current) / name
            if name == ".rar-extraction-lock" and path.parent != root:
                raise rar.ExtractionError("Existe reserva de extração em uma subpasta; reset interrompido.")
            if path.is_mount() or path.stat().st_dev != device:
                raise rar.ExtractionError("Há outro disco montado dentro da origem; reset interrompido.")


def _clear(root: Path, backup: Path) -> None:
    for path in sorted(root.iterdir()):
        if path == backup or path.name == ".rar-extraction-lock":
            continue
        if path.parent.resolve() != root or not path.absolute().is_relative_to(root):
            raise rar.ExtractionError("Alvo de exclusão fora da origem.")
        print(f"Removendo da origem: {path.name}", flush=True)
        if path.is_symlink():
            path.unlink()
        elif hasattr(path, "is_junction") and path.is_junction():
            path.rmdir()
        elif path.is_dir():
            if path.is_mount() or not path.resolve().is_relative_to(root):
                raise rar.ExtractionError("Alvo de exclusão fora da origem ou ponto de montagem.")
            shutil.rmtree(path)
        else:
            path.unlink()


def reset(folder: Path, apply: bool = False) -> dict:
    folder = Path(folder).absolute()
    if _link(folder):
        raise rar.ExtractionError("Escolha uma pasta real, sem link.")
    root = folder.resolve(strict=True)
    if not root.is_dir() or root.parent == root or any(part.casefold() == "backup" for part in root.parts):
        raise rar.ExtractionError("Pasta de dados inválida; não use a raiz nem uma pasta dentro de backup.")
    backup = root / "backup"
    if _link(backup) or not backup.is_dir():
        raise rar.ExtractionError("backup precisa existir como pasta real; nada será apagado.")
    for tool in ("lsar", "unrar"):
        if shutil.which(tool) is None:
            raise rar.ExtractionError("Instale as ferramentas: sudo apt install unar unrar")
    for parent in (root, *root.parents):
        if (parent / ".rar-extraction-lock").exists():
            raise rar.ExtractionError("Há uma reserva de extração ativa ou pendente; confira antes de refazer.")
    lock = root / ".rar-extraction-lock"
    lock.mkdir()
    run = None
    report = {"root": str(root), "status": "planning", "extracted_sets": 0}
    try:
        groups, skipped = _plan(backup)
        report.update(archives=[{"source": str(group["archive"].relative_to(backup)),
                                 "output": str(group["output"])} for group in groups], skipped=skipped,
                      remove=[path.name for path in sorted(root.iterdir()) if path != backup and path != lock])
        _preflight_tree(root, backup)
        if not apply:
            report["status"] = "preview"
            return report
        run = backup / ("reset-extracao-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        run.mkdir()
        report["report"] = str(run / "report.json")
        report["status"] = "testing_backup"
        _write_report(run, report)
        for index, group in enumerate(groups):
            print(f"Testando integridade completa: {group['archive'].name}", flush=True)
            _test(group["archive"], run / f"test-{index:04}.log")
            if any(_identity(path) != identity for path, identity in group["identities"].items()):
                raise rar.ExtractionError("Um volume mudou durante o teste; nada será apagado.")
        report["status"] = "clearing"
        _write_report(run, report)
        _clear(root, backup)
        report["status"] = "extracting"
        _write_report(run, report)
        fingerprints = set()
        for group in groups:
            rar._extract_set(group["archive"], group["volumes"], group["expanded"], root, run, "unrar",
                             output_override=root / group["output"], keep_sources=True)
            fingerprints.add(group["fingerprint"])
            report["extracted_sets"] += 1
            _write_report(run, report)
        seen = set()
        for _ in range(12):
            candidates = [path for path in rar._discover(root) if path not in seen]
            if not candidates:
                break
            for archive in candidates:
                if not archive.exists() or archive in seen:
                    continue
                volumes, expanded = rar._inspect(archive)
                seen.update(volumes)
                fingerprint = _fingerprint(volumes)
                if fingerprint in fingerprints:
                    for volume in volumes:
                        destination = run / "nested-copies" / volume.relative_to(root)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if destination.exists():
                            raise rar.ExtractionError("Conflito ao guardar um volume aninhado repetido.")
                        volume.rename(destination)
                    continue
                rar._extract_set(volumes[0], volumes, expanded, root, run / "nested", "unrar")
                fingerprints.add(fingerprint)
                report["extracted_sets"] += 1
                _write_report(run, report)
        if rar._discover(root):
            raise rar.ExtractionError("Limite de aninhamento atingido; backup e saídas atuais preservados.")
        report["status"] = "ok"
        _write_report(run, report)
        return report
    except (rar.ExtractionError, OSError) as error:
        report.update(status="failed", error=str(error))
        if isinstance(error, rar.ExtractionError):
            report.update(error.details)
        if run is not None:
            _write_report(run, report)
        raise
    finally:
        lock.rmdir()


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--folder", required=True, type=Path)
    cli.add_argument("--apply", action="store_true", help="APAGA tudo fora de backup e refaz as extrações")
    args = cli.parse_args()
    try:
        print(json.dumps(reset(args.folder, args.apply), ensure_ascii=False, indent=2))
        return 0
    except (rar.ExtractionError, OSError) as error:
        print(f"Erro: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
