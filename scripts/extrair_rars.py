"""Extract RAR sets with unar; archive originals only after successful extraction."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import uuid


class ExtractionError(ValueError):
    pass


def _inspect(archive: Path) -> tuple[list[Path], int]:
    # A file bounds memory use for very large archive listings.
    with tempfile.TemporaryFile() as listing:
        result = subprocess.run(["lsar", "-j", "-nr", str(archive)],
                                stdin=subprocess.DEVNULL, stdout=listing, stderr=subprocess.DEVNULL)
        if result.returncode or listing.tell() > 64 * 1024**2:
            raise ExtractionError("Não foi possível listar o RAR; confira integridade, senha e partes faltantes.")
        listing.seek(0)
        try:
            info = json.load(listing)
        except (ValueError, UnicodeError):
            raise ExtractionError("Listagem de arquivo inválida.") from None
    if not isinstance(info, dict) or info.get("lsarError") or not str(info.get("lsarFormatName", "")).upper().startswith("RAR"):
        raise ExtractionError("Arquivo não reconhecido como RAR íntegro.")
    properties = info.get("lsarProperties", {})
    if not isinstance(properties, dict) or properties.get("XADVolumeScanningFailed"):
        raise ExtractionError("Não foi possível identificar todas as partes do RAR.")
    raw_volumes = properties.get("XADVolumes") or [str(archive)]
    if not isinstance(raw_volumes, list) or not raw_volumes:
        raise ExtractionError("Lista de volumes inválida.")
    volumes = []
    for value in raw_volumes:
        if not isinstance(value, str):
            raise ExtractionError("Nome de volume inválido.")
        volume = Path(value)
        if not volume.is_absolute():
            volume = archive.parent / volume
        if volume.is_symlink() or volume.resolve().parent != archive.parent or not volume.is_file():
            raise ExtractionError("Volume ausente ou localizado fora da pasta do conjunto.")
        if volume.resolve() not in volumes:
            volumes.append(volume.resolve())
    if archive not in volumes:
        raise ExtractionError("O RAR atual não consta na lista de volumes.")
    entries = info.get("lsarContents")
    if not isinstance(entries, list) or not entries or len(entries) > 100_000:
        raise ExtractionError("RAR vazio ou com quantidade de membros não suportada.")
    expanded = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExtractionError("Membro de RAR inválido.")
        name = entry.get("XADFileName")
        if not isinstance(name, str) or not name or "\0" in name:
            raise ExtractionError("Nome de membro inválido.")
        normalized = name.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if normalized.startswith("/") or ".." in parts or any(":" in part for part in parts):
            raise ExtractionError("Caminho de extração inseguro.")
        if (entry.get("XADIsLink") or entry.get("XADLinkDestination")
                or entry.get("XADIsHardLink") or entry.get("XADIsUnixSymlink")
                or any(entry.get(key) for key in ("XADIsFIFO", "XADIsCharacterDevice", "XADIsBlockDevice", "XADIsSocket"))):
            raise ExtractionError("Links e arquivos especiais dentro de RARs não são extraídos por este script.")
        mode = entry.get("XADPosixPermissions", 0)
        if type(mode) is not int or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ExtractionError("Tipo especial de membro recusado.")
        if not entry.get("XADIsDirectory"):
            size = entry.get("XADFileSize")
            if type(size) is not int or size < 0:
                raise ExtractionError("Não foi possível estimar o espaço necessário.")
            expanded += size
    return volumes, expanded


def _discover(root: Path) -> list[Path]:
    result = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories
                                if name.casefold() != "backup"
                                and not name.startswith((".rar-pending-", ".rar-extraction-lock"))
                                and not (Path(current) / name).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.casefold() == ".rar" and not path.is_symlink():
                result.append(path)
    return sorted(result)


def _extract_set(archive: Path, volumes: list[Path], expanded: int, root: Path, backup_run: Path) -> Path:
    output = archive.parent / (archive.stem + "_extraido")
    if output.exists() or output.is_symlink():
        raise ExtractionError("A pasta de saída já existe; confira seu conteúdo antes de repetir.")
    if expanded > shutil.disk_usage(archive.parent).free:
        raise ExtractionError("Espaço livre insuficiente para a extração estimada.")
    identities = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in volumes}
    staging = Path(tempfile.mkdtemp(prefix=".rar-pending-", dir=archive.parent))
    result = subprocess.run(["unar", "-nr", "-D", "-o", str(staging), str(archive)], stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ExtractionError(f"Extração falhou. RARs preservados; saída parcial em {staging.name}.")
    for current, directories, files in os.walk(staging, followlinks=False):
        for name in directories + files:
            mode = (Path(current) / name).lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ExtractionError(f"Saída contém arquivo especial; originais preservados e saída em {staging.name}.")
    if any(path.is_symlink() or (path.stat().st_size, path.stat().st_mtime_ns) != identity
           for path, identity in identities.items()):
        raise ExtractionError(f"Uma parte mudou durante a extração; originais preservados e saída em {staging.name}.")
    destinations = {path: backup_run / path.relative_to(root) for path in volumes}
    for destination in destinations.values():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise ExtractionError("Já existe um volume no destino de backup; operação interrompida.")
    # Keep successfully extracted data even if a later archive move fails.
    staging.rename(output)
    moved = []
    try:
        for source, destination in destinations.items():
            source.rename(destination)
            moved.append((source, destination))
    except OSError:
        for source, destination in reversed(moved):
            if not source.exists() and not source.is_symlink():
                destination.rename(source)
        raise ExtractionError("Falha ao guardar volumes; confira a origem e backup. A saída extraída foi preservada.") from None
    return output


def extract_folder(folder: Path, max_depth: int = 12) -> dict:
    if max_depth < 1:
        raise ExtractionError("max-depth deve ser positivo.")
    root = folder.resolve(strict=True)
    if not root.is_dir() or root.parent == root or root.name.casefold() == "backup":
        raise ExtractionError("Escolha a pasta dos dados, fora de backup.")
    for tool in ("lsar", "unar"):
        if shutil.which(tool) is None:
            raise ExtractionError("Instale as ferramentas no Linux: sudo apt install unar")
    backup = root / "backup"
    if backup.is_symlink():
        raise ExtractionError("backup não pode ser um link simbólico.")
    backup.mkdir(exist_ok=True)
    lock = root / ".rar-extraction-lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ExtractionError("Há outra execução ou reserva pendente em .rar-extraction-lock.") from None
    report = {"extracted_sets": 0, "backed_up_volumes": 0, "failures": []}
    backup_run = backup / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    seen = set()
    try:
        for depth in range(max_depth):
            candidates = [path for path in _discover(root) if path not in seen]
            if not candidates:
                break
            for archive in candidates:
                if not archive.exists() or archive in seen:
                    continue
                print(f"Analisando: {archive.relative_to(root)}", flush=True)
                seen.add(archive)
                try:
                    volumes, expanded = _inspect(archive)
                    seen.update(volumes)
                    output = _extract_set(volumes[0], volumes, expanded, root, backup_run)
                    report["extracted_sets"] += 1
                    report["backed_up_volumes"] += len(volumes)
                    print(f"Concluído: {output.relative_to(root)}", flush=True)
                except (ExtractionError, OSError) as error:
                    report["failures"].append({"archive": archive.relative_to(root).as_posix(), "error": str(error)})
                    print(f"Preservado por erro: {archive.relative_to(root)}: {error}", flush=True)
        remaining = _discover(root)
        report["remaining_rars"] = len(remaining)
        if remaining and not report["failures"]:
            report["failures"].append({"error": "Limite de aninhamento atingido; RARs restantes foram preservados."})
        return report
    finally:
        lock.rmdir()


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--folder", type=Path, default=Path.cwd())
    cli.add_argument("--max-depth", type=int, default=12)
    args = cli.parse_args()
    try:
        report = extract_folder(args.folder, args.max_depth)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["failures"] else 0
    except (ExtractionError, OSError) as error:
        print(f"Erro: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
