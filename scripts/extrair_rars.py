"""Extract RAR sets with unar or unrar; back up originals only after success."""
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
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = dict(details or {})


def _diagnostic_tail(stream, limit: int = 4096) -> str:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - limit))
    value = stream.read(limit).decode("utf-8", errors="replace").strip()
    # Preserve readable diagnostics without forwarding terminal control codes.
    return "".join(char if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
                   else f"\\x{ord(char):02x}" for char in value)


def _save_report(backup: Path, report: dict) -> None:
    temporary = backup / (".report-" + uuid.uuid4().hex + ".json")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(backup / "last-extraction-report.json")
    finally:
        temporary.unlink(missing_ok=True)


def _inspect(archive: Path) -> tuple[list[Path], int]:
    # A file bounds memory use for very large archive listings.
    with tempfile.TemporaryFile() as listing, tempfile.TemporaryFile() as errors:
        result = subprocess.run(["lsar", "-j", "-nr", str(archive)],
                                stdin=subprocess.DEVNULL, stdout=listing, stderr=errors)
        if listing.tell() > 64 * 1024**2:
            raise ExtractionError("A listagem do RAR excede o limite de 64 MiB.")
        if result.returncode:
            diagnostic = _diagnostic_tail(errors) or _diagnostic_tail(listing)
            raise ExtractionError(
                f"lsar falhou com código {result.returncode}. "
                + (diagnostic or "Confira integridade, senha e partes faltantes."),
                details={"tool": "lsar", "tool_exit_code": result.returncode, "diagnostic": diagnostic},
            )
        listing.seek(0)
        try:
            info = json.load(listing)
        except (ValueError, UnicodeError):
            raise ExtractionError("Listagem de arquivo inválida.") from None
    if not isinstance(info, dict):
        raise ExtractionError("Listagem do RAR não contém um objeto JSON.")
    if info.get("lsarError"):
        raise ExtractionError(f"lsar informou erro interno {info['lsarError']}.")
    detected_format = str(info.get("lsarFormatName", ""))
    if not detected_format.upper().startswith("RAR"):
        raise ExtractionError(f"Formato encontrado: {detected_format!r}; este script processa somente RAR.")
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
    names = {}
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
        canonical = "/".join(parts).casefold()
        directory = bool(entry.get("XADIsDirectory"))
        if canonical in names and not (directory and names[canonical]):
            raise ExtractionError("Membros com nomes repetidos ou equivalentes; extração recusada para evitar omissões.")
        names[canonical] = directory
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


def _extract_set(archive: Path, volumes: list[Path], expanded: int, root: Path, backup_run: Path,
                 backend: str = "unar", *, output_override: Path | None = None,
                 keep_sources: bool = False) -> Path:
    output = output_override or archive.parent / (archive.stem + "_extraido")
    if output.resolve() == root.resolve() or not output.resolve().is_relative_to(root.resolve()):
        raise ExtractionError("A saída precisa ficar dentro da pasta de dados.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise ExtractionError(f"A pasta de saída já existe: {output.name}. Confira seu conteúdo antes de repetir.")
    free = shutil.disk_usage(output.parent).free
    if expanded > free:
        raise ExtractionError(f"Espaço livre insuficiente: necessários aproximadamente {expanded} bytes; livres {free} bytes.")
    identities = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in volumes}
    staging = Path(tempfile.mkdtemp(prefix=".rar-pending-", dir=output.parent))
    log_directory = backup_run / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / (uuid.uuid4().hex + f".{backend}.log")
    print(f"Extraindo; acompanhe o log: {log_path}", flush=True)
    if backend == "unrar":
        command = ["unrar", "x", "-cfg-", "-o-", "-p-", str(archive), str(staging) + os.sep]
    else:
        command = ["unar", "-nr", "-D", "-o", str(staging), str(archive)]
    with log_path.open("x+b") as log:
        result = subprocess.run(command,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                env={key: value for key, value in os.environ.items() if key != "RAR"}
                                if backend == "unrar" else None)
        if result.returncode:
            diagnostic = _diagnostic_tail(log)
            raise ExtractionError(
                f"{backend} falhou com código {result.returncode}. "
                f"RARs preservados; saída parcial em {staging.name}. Log: {log_path}.\n"
                + (diagnostic or "O descompactador não forneceu detalhes adicionais."),
                details={"tool": backend, "tool_exit_code": result.returncode,
                         "log": str(log_path), "partial_output": str(staging), "diagnostic": diagnostic},
            )
    for current, directories, files in os.walk(staging, followlinks=False):
        for name in directories + files:
            mode = (Path(current) / name).lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ExtractionError(f"Saída contém arquivo especial; originais preservados e saída em {staging.name}.")
    if any(path.is_symlink() or (path.stat().st_size, path.stat().st_mtime_ns) != identity
           for path, identity in identities.items()):
        raise ExtractionError(f"Uma parte mudou durante a extração; originais preservados e saída em {staging.name}.")
    if keep_sources:
        staging.rename(output)
        return output
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


def extract_folder(folder: Path, max_depth: int = 12, backend: str = "unar") -> dict:
    if max_depth < 1:
        raise ExtractionError("max-depth deve ser positivo.")
    if backend not in ("unar", "unrar"):
        raise ExtractionError("backend deve ser unar ou unrar.")
    root = folder.resolve(strict=True)
    if not root.is_dir() or root.parent == root or root.name.casefold() == "backup":
        raise ExtractionError("Escolha a pasta dos dados, fora de backup.")
    for tool in ("lsar", backend):
        if shutil.which(tool) is None:
            raise ExtractionError("Instale as ferramentas no Linux: sudo apt install unar unrar")
    backup = root / "backup"
    if backup.is_symlink():
        raise ExtractionError("backup não pode ser um link simbólico.")
    backup.mkdir(exist_ok=True)
    lock = root / ".rar-extraction-lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ExtractionError("Há outra execução ou reserva pendente em .rar-extraction-lock.") from None
    report = {"backend": backend, "extracted_sets": 0, "backed_up_volumes": 0, "failures": []}
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
                    output = _extract_set(volumes[0], volumes, expanded, root, backup_run, backend)
                    report["extracted_sets"] += 1
                    report["backed_up_volumes"] += len(volumes)
                    print(f"Concluído: {output.relative_to(root)}", flush=True)
                except (ExtractionError, OSError) as error:
                    failure = {"archive": archive.relative_to(root).as_posix(), "error": str(error)}
                    if isinstance(error, ExtractionError):
                        failure.update(error.details)
                    report["failures"].append(failure)
                    print(f"Preservado por erro: {archive.relative_to(root)}: {error}", flush=True)
        remaining = _discover(root)
        report["remaining_rars"] = len(remaining)
        if remaining and not report["failures"]:
            report["failures"].append({"error": "Limite de aninhamento atingido; RARs restantes foram preservados."})
        _save_report(backup, report)
        print(f"Relatório salvo: {backup / 'last-extraction-report.json'}", flush=True)
        return report
    finally:
        lock.rmdir()


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--folder", type=Path, default=Path.cwd())
    cli.add_argument("--max-depth", type=int, default=12)
    cli.add_argument("--backend", choices=("unar", "unrar"), default="unar",
                     help="Descompactador usado; lsar continua necessário para a listagem")
    args = cli.parse_args()
    try:
        report = extract_folder(args.folder, args.max_depth, args.backend)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["failures"] else 0
    except (ExtractionError, OSError) as error:
        print(f"Erro: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
