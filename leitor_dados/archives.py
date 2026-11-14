"""Discover supported inputs and expand archives into a separate, bounded cache.

Original files are only read. Expansion limits include every archive member and
every logical use of a cached archive; direct CSV/SQLite inputs are not charged
to the expansion budget. Archives are never executed or extracted with a shell.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import struct
import tarfile
from typing import BinaryIO
import uuid
import zipfile


CHUNK_SIZE = 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_INDEX_LINE_BYTES = 32 * 1024
MAX_ZIP_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_TAR_METADATA_BYTES = 1024 * 1024
MAX_RAR_METADATA_BYTES = 64 * 1024 * 1024
SUPPORTED_FILES = frozenset({".csv", ".tsv", ".txt", ".db", ".sqlite", ".sqlite3", ".jsonl", ".ndjson"})
IGNORED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__"})


class ArchiveError(ValueError):
    """An input archive or its managed cache cannot be used safely."""


class UnsafeArchiveError(ArchiveError):
    pass


class ExtractionLimitError(ArchiveError):
    pass


class UnsupportedArchive(ArchiveError):
    pass


@dataclass(frozen=True)
class ExtractionLimits:
    max_depth: int = 12
    max_members: int = 100_000
    max_expanded_bytes: int = 100 * 1024**3
    max_single_file_bytes: int = 20 * 1024**3

    def __post_init__(self) -> None:
        for value in (self.max_depth, self.max_members, self.max_expanded_bytes, self.max_single_file_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Os limites de extração devem ser inteiros positivos.")


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path
    logical_parent: str
    source: str


@dataclass
class _Budget:
    limits: ExtractionLimits
    members: int = 0
    expanded: int = 0

    def check_size(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ArchiveError("Tamanho de membro inválido.")
        if size > self.limits.max_single_file_bytes:
            raise ExtractionLimitError("Um membro excede max_single_file_bytes.")
        if self.expanded + size > self.limits.max_expanded_bytes:
            raise ExtractionLimitError("A expansão excede max_expanded_bytes.")

    def member(self, size: int) -> None:
        if self.members >= self.limits.max_members:
            raise ExtractionLimitError("A expansão excede max_members.")
        self.check_size(size)
        self.members += 1

    def chunk(self, size: int, file_size: int) -> None:
        if file_size > self.limits.max_single_file_bytes:
            raise ExtractionLimitError("Um membro excede max_single_file_bytes durante a leitura.")
        if self.expanded + size > self.limits.max_expanded_bytes:
            raise ExtractionLimitError("A expansão excede max_expanded_bytes durante a leitura.")
        self.expanded += size


def _member_name(value: str) -> str:
    if not isinstance(value, str):
        raise UnsafeArchiveError("Nome de membro inválido.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise UnsafeArchiveError("Nome de membro não é UTF-8 válido.") from None
    if (not value or len(encoded) > MAX_PATH_BYTES or "\x00" in value or "\\" in value
            or PureWindowsPath(value).drive or value.startswith("/")):
        raise UnsafeArchiveError("Caminho absoluto, ambíguo ou inválido no arquivo compactado.")
    value = value[:-1] if value.endswith("/") else value
    while value.startswith("./"):
        value = value[2:]
    if value == ".":
        return "."
    parts = value.split("/")
    if not value or any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise UnsafeArchiveError("Travessia ou componente inválido no arquivo compactado.")
    return "/".join(parts)


class _Names:
    """Reject duplicate members and case/ancestor collisions on every platform."""

    def __init__(self) -> None:
        self.explicit: set[str] = set()
        self.paths: dict[str, tuple[str, bool]] = {}

    def add(self, name: str, is_dir: bool) -> None:
        key = name.casefold()
        if key in self.explicit:
            raise UnsafeArchiveError("Membros duplicados ou com diferença apenas de caixa.")
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            current = "/".join(parts[:index])
            directory = index < len(parts) or is_dir
            previous = self.paths.get(current.casefold())
            if previous is not None and previous != (current, directory):
                raise UnsafeArchiveError("Colisão de caminhos de membros do arquivo compactado.")
            self.paths[current.casefold()] = current, directory
        self.explicit.add(key)


def _regular_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeArchiveError("Links e arquivos especiais não são entradas permitidas.")
    return metadata


def _hash_file(path: Path) -> str:
    _regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _kind(path: Path) -> str | None:
    name = path.name.casefold()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".rar"):
        return "rar"
    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2", ".tar.xz", ".txz")):
        return "tar"
    return None


def _zip_preflight(path: Path, remaining_members: int) -> None:
    """Bound central-directory metadata before ZipFile loads its member list."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        length = stream.tell()
        stream.seek(max(0, length - 65_557))
        tail = stream.read(65_557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise ArchiveError("ZIP inválido ou incompleto.")
        end = struct.unpack_from("<4s4H2IH", tail, offset)
        if offset + 22 + end[7] != len(tail) or end[1] or end[2]:
            raise UnsupportedArchive("ZIP multipartes ou diretório inválido não é suportado.")
        count, size, start = end[4], end[5], end[6]
        end_position = length - len(tail) + offset
        if count == 65_535 or size == 0xFFFFFFFF or start == 0xFFFFFFFF:
            stream.seek(end_position - 20)
            locator = stream.read(20)
            if len(locator) != 20:
                raise ArchiveError("Diretório ZIP64 inválido.")
            signature, disk, position, disks = struct.unpack("<4sIQI", locator)
            if signature != b"PK\x06\x07" or disk or disks != 1:
                raise UnsupportedArchive("ZIP64 multipartes não é suportado.")
            stream.seek(position)
            record = stream.read(56)
            if len(record) != 56:
                raise ArchiveError("Diretório ZIP64 incompleto.")
            values = struct.unpack("<4sQ2H2I4Q", record)
            if values[0] != b"PK\x06\x06" or values[4] or values[5]:
                raise ArchiveError("Diretório ZIP64 inválido.")
            count, size, start = values[7], values[8], values[9]
            end_position = position
        if count > remaining_members:
            raise ExtractionLimitError("O diretório ZIP excede max_members.")
        if size > MAX_ZIP_DIRECTORY_BYTES:
            raise ExtractionLimitError("Metadados ZIP excedem o limite seguro de 64 MiB.")
        if start < 0 or start + size != end_position:
            raise ArchiveError("Posição do diretório ZIP inválida.")
        stream.seek(start)
        observed = 0
        while stream.tell() < end_position:
            header = stream.read(46)
            if len(header) != 46:
                raise ArchiveError("Cabeçalho ZIP incompleto.")
            fields = struct.unpack("<4s6H3I5H2I", header)
            if fields[0] != b"PK\x01\x02":
                raise ArchiveError("Cabeçalho ZIP inválido.")
            observed += 1
            if observed > remaining_members or observed > count:
                raise ExtractionLimitError("O diretório ZIP excede a contagem declarada ou max_members.")
            if fields[10] > MAX_PATH_BYTES:
                raise UnsafeArchiveError("Nome ZIP excede o limite seguro de caminho.")
            name = stream.read(fields[10])
            try:
                _member_name(name.decode("utf-8" if fields[3] & 0x800 else "cp437", errors="strict"))
            except UnicodeError:
                raise UnsafeArchiveError("Nome ZIP não é UTF-8 válido.") from None
            stream.seek(fields[11] + fields[12], os.SEEK_CUR)
            if stream.tell() > end_position:
                raise ArchiveError("Metadados ZIP ultrapassam o diretório declarado.")
        if observed != count:
            raise ArchiveError("Contagem de membros ZIP divergente.")


def _copy_member(source: BinaryIO, target: Path, declared: int, budget: _Budget) -> int:
    actual = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            actual += len(chunk)
            budget.chunk(len(chunk), actual)
            if actual > declared:
                raise ArchiveError("Conteúdo expandido excede o tamanho declarado.")
            output.write(chunk)
    if actual != declared:
        raise ArchiveError("Conteúdo expandido não corresponde ao tamanho declarado.")
    return actual


def _write_entry(index, files: Path, names: _Names, name: str, size: int,
                 is_dir: bool, source: BinaryIO | None, budget: _Budget) -> int:
    name = _member_name(name)
    if name == "." and not is_dir:
        raise UnsafeArchiveError("O membro raiz de um arquivo deve ser um diretório.")
    names.add(name, is_dir)
    budget.member(size)
    target = files.joinpath(*name.split("/"))
    if is_dir:
        if size:
            raise ArchiveError("Diretório de arquivo compactado contém tamanho inválido.")
        target.mkdir(parents=True, exist_ok=True)
    else:
        if source is None:
            raise ArchiveError("Não foi possível abrir o conteúdo de um membro.")
        _copy_member(source, target, size, budget)
    index.write(json.dumps({"name": name, "size": size, "directory": is_dir}, ensure_ascii=True) + "\n")
    return size


def _extract_zip(path: Path, files: Path, index, budget: _Budget) -> tuple[int, int]:
    _zip_preflight(path, budget.limits.max_members - budget.members)
    names, count, total = _Names(), 0, 0
    with zipfile.ZipFile(path, "r") as archive:
        for member in archive.infolist():
            if member.flag_bits & 1:
                raise UnsupportedArchive("ZIP criptografado requer senha; entrada não importada.")
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UnsafeArchiveError("Links e arquivos especiais ZIP não são permitidos.")
            is_dir = member.is_dir() or file_type == stat.S_IFDIR
            if is_dir:
                total += _write_entry(index, files, names, member.filename, member.file_size, True, None, budget)
            else:
                budget.check_size(member.file_size)
                with archive.open(member, "r") as stream:
                    total += _write_entry(index, files, names, member.filename, member.file_size, False, stream, budget)
            count += 1
    return count, total


class _SafeTarInfo(tarfile.TarInfo):
    """Bound metadata reads that tarfile otherwise performs before payload iteration."""

    def _metadata(self, archive, processor):
        if self.size > MAX_TAR_METADATA_BYTES:
            raise ExtractionLimitError("Metadados TAR excedem o limite seguro de 1 MiB.")
        depth = getattr(archive, "_metadata_depth", 0)
        if depth >= 32:
            raise ExtractionLimitError("Encadeamento de metadados TAR excede o limite seguro.")
        archive._metadata_depth = depth + 1
        try:
            member = processor(archive)
            for headers in (archive.pax_headers, getattr(member, "pax_headers", {})):
                if sum(len(str(key)) + len(str(value)) for key, value in headers.items()) > MAX_TAR_METADATA_BYTES:
                    raise ExtractionLimitError("Metadados acumulados TAR excedem o limite seguro.")
            return member
        finally:
            archive._metadata_depth = depth

    def _proc_pax(self, archive):
        return self._metadata(archive, super()._proc_pax)

    def _proc_gnulong(self, archive):
        return self._metadata(archive, super()._proc_gnulong)

    def _proc_sparse(self, archive):
        raise UnsupportedArchive("TAR com arquivo esparso GNU não é suportado.")

    def _proc_gnusparse_00(self, *args, **kwargs):
        raise UnsupportedArchive("TAR com arquivo esparso GNU não é suportado.")

    def _proc_gnusparse_01(self, *args, **kwargs):
        raise UnsupportedArchive("TAR com arquivo esparso GNU não é suportado.")

    def _proc_gnusparse_10(self, *args, **kwargs):
        raise UnsupportedArchive("TAR com arquivo esparso GNU não é suportado.")


def _extract_tar(path: Path, files: Path, index, budget: _Budget) -> tuple[int, int]:
    names, count, total = _Names(), 0, 0
    with tarfile.open(path, "r|*", encoding="utf-8", errors="surrogateescape", tarinfo=_SafeTarInfo) as archive:
        while (member := archive.next()) is not None:
            if member.sparse is not None:
                raise UnsupportedArchive("TAR com arquivo esparso não é suportado.")
            if not member.isdir() and not member.isfile():
                raise UnsafeArchiveError("Links, dispositivos e arquivos especiais TAR não são permitidos.")
            if member.isdir():
                total += _write_entry(index, files, names, member.name, member.size, True, None, budget)
            else:
                budget.check_size(member.size)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArchiveError("Conteúdo TAR ausente.")
                with stream:
                    total += _write_entry(index, files, names, member.name, member.size, False, stream, budget)
            count += 1
            # Hard links are rejected, so retaining prior TarInfo objects is unnecessary.
            archive.members.clear()
    return count, total


def _vint(stream: BinaryIO) -> tuple[int, int]:
    value = 0
    for position in range(10):
        byte = stream.read(1)
        if len(byte) != 1:
            raise ArchiveError("Inteiro RAR5 incompleto.")
        value |= (byte[0] & 0x7F) << (position * 7)
        if not byte[0] & 0x80:
            return value, position + 1
    raise ExtractionLimitError("Inteiro RAR5 excede o limite seguro de 10 bytes.")


def _rar5_file_metadata(fields: BinaryIO, length: int) -> None:
    """Validate every variable integer the pinned rarfile parser consumes."""
    import io

    def fixed(stream: BinaryIO, size: int) -> bytes:
        data = stream.read(size)
        if len(data) != size:
            raise ArchiveError("Metadados RAR5 incompletos.")
        return data

    def text(stream: BinaryIO) -> bytes:
        size = _vint(stream)[0]
        if size > MAX_PATH_BYTES:
            raise ExtractionLimitError("Nome em metadados RAR5 excede o limite seguro.")
        return fixed(stream, size)

    file_flags = _vint(fields)[0]
    _vint(fields)  # expanded size
    _vint(fields)  # attributes
    if file_flags & 2:
        fixed(fields, 4)  # mtime
    if file_flags & 4:
        fixed(fields, 4)  # CRC
    _vint(fields)  # compression
    _vint(fields)  # host OS
    try:
        _member_name(text(fields).decode("utf-8", errors="strict"))
    except UnicodeError:
        raise UnsafeArchiveError("Nome RAR5 não é UTF-8 válido.") from None
    while fields.tell() < length - 1:
        size = _vint(fields)[0]
        if size > length - fields.tell():
            raise ArchiveError("Área adicional RAR5 inválida.")
        extra = io.BytesIO(fixed(fields, size))
        extra_type = _vint(extra)[0]
        if extra_type == 1:
            raise UnsupportedArchive("RAR criptografado requer senha; entrada não importada.")
        if extra_type == 2:  # hash type; remainder is fixed-width hash data
            _vint(extra)
        elif extra_type == 3:  # time flags; remainder is fixed-width time data
            _vint(extra)
        elif extra_type == 4:  # historical file version
            _vint(extra)
            _vint(extra)
        elif extra_type == 5:
            raise UnsafeArchiveError("Links e redirecionamentos RAR5 não são permitidos.")
        elif extra_type == 6:
            flags = _vint(extra)[0]
            if flags & 1:
                text(extra)
            if flags & 2:
                text(extra)
            if flags & 4:
                _vint(extra)
            if flags & 8:
                _vint(extra)


def _rar_preflight(path: Path, remaining_members: int) -> None:
    """Bound raw metadata before rarfile parses it or follows another volume."""
    import io
    with path.open("rb") as stream:
        signature = stream.read(8)
        if signature == b"Rar!\x1a\x07\x01\x00":
            modern = True
        elif signature[:7] == b"Rar!\x1a\x07\x00":
            modern = False
            stream.seek(7)
        else:
            raise UnsupportedArchive("Assinatura RAR inválida; arquivos autoextraíveis não são suportados.")
        length = path.stat().st_size
        count = metadata_bytes = 0
        while stream.tell() < length:
            count += 1
            if count > remaining_members + 1024:
                raise ExtractionLimitError("A quantidade de cabeçalhos RAR excede o limite seguro.")
            if modern:
                if len(stream.read(4)) != 4:
                    raise ArchiveError("Cabeçalho RAR5 incompleto.")
                size, vint_size = _vint(stream)
                if size > 2 * 1024 * 1024:
                    raise ExtractionLimitError("Cabeçalho RAR5 excede o limite seguro de 2 MiB.")
                metadata_bytes += size + vint_size + 4
                if metadata_bytes > MAX_RAR_METADATA_BYTES:
                    raise ExtractionLimitError("Metadados RAR excedem o limite seguro de 64 MiB.")
                header = stream.read(size)
                if len(header) != size:
                    raise ArchiveError("Cabeçalho RAR5 incompleto.")
                fields = io.BytesIO(header)
                block_type, _ = _vint(fields)
                flags, _ = _vint(fields)
                if flags & 1:
                    _vint(fields)  # extra-area size
                data_size = _vint(fields)[0] if flags & 2 else 0
                if block_type == 4:
                    raise UnsupportedArchive("RAR criptografado requer senha; entrada não importada.")
                if flags & (8 | 16):
                    raise UnsupportedArchive("RAR multipartes não é suportado nesta versão.")
                if block_type == 1:
                    main_flags = _vint(fields)[0]
                    if main_flags & 1:
                        raise UnsupportedArchive("RAR multipartes não é suportado nesta versão.")
                    if main_flags & 2:
                        _vint(fields)
                elif block_type in (2, 3):
                    _rar5_file_metadata(fields, len(header))
                elif block_type == 5:
                    _vint(fields)
                ended = block_type == 5
            else:
                raw = stream.read(7)
                if len(raw) != 7:
                    raise ArchiveError("Cabeçalho RAR3 incompleto.")
                _, block_type, flags, size = struct.unpack("<HBHH", raw)
                if size < 7:
                    raise ArchiveError("Tamanho de cabeçalho RAR3 inválido.")
                metadata_bytes += size
                if metadata_bytes > MAX_RAR_METADATA_BYTES:
                    raise ExtractionLimitError("Metadados RAR excedem o limite seguro de 64 MiB.")
                header = stream.read(size - 7)
                if len(header) != size - 7:
                    raise ArchiveError("Cabeçalho RAR3 incompleto.")
                data_size = 0
                if flags & 0x8000:
                    if len(header) < 4:
                        raise ArchiveError("Cabeçalho RAR3 sem tamanho de dados.")
                    data_size = struct.unpack_from("<I", header)[0]
                if block_type == 0x74 and flags & 0x100:
                    if len(header) < 33:
                        raise ArchiveError("Cabeçalho RAR3 de arquivo grande inválido.")
                    data_size |= struct.unpack_from("<I", header, 25)[0] << 32
                if (block_type == 0x73 and flags & 0x80) or (block_type == 0x74 and flags & 4):
                    raise UnsupportedArchive("RAR criptografado requer senha; entrada não importada.")
                if block_type == 0x73 and flags & 1:
                    raise UnsupportedArchive("RAR multipartes não é suportado nesta versão.")
                ended = block_type == 0x7B
            if stream.tell() + data_size > length:
                raise ArchiveError("Dados RAR ultrapassam o tamanho do arquivo.")
            stream.seek(data_size, os.SEEK_CUR)
            if ended:
                return


def _extract_rar(path: Path, files: Path, index, budget: _Budget) -> tuple[int, int]:
    try:
        import rarfile
    except ImportError:
        raise UnsupportedArchive("RAR exige o pacote rarfile e backend unar ou unrar.") from None
    if not (shutil.which("unar") or shutil.which("unrar")):
        raise UnsupportedArchive("RAR encontrado, mas falta instalar unar ou unrar no servidor.")
    _rar_preflight(path, budget.limits.max_members - budget.members)
    scanned, declared = 0, 0

    def inspect_header(info) -> None:
        nonlocal scanned, declared
        if getattr(info, "filename", None) is None:
            return
        scanned += 1
        if scanned > budget.limits.max_members - budget.members:
            raise ExtractionLimitError("O diretório RAR excede max_members.")
        original_name = getattr(info, "orig_filename", None)
        unicode_name = (hasattr(info, "file_host_os")
                        or (getattr(info, "flags", 0) & rarfile.RAR_FILE_UNICODE
                            and isinstance(original_name, bytes) and b"\x00" not in original_name))
        if unicode_name and isinstance(original_name, bytes):
            try:
                original_name.decode("utf-8", errors="strict")
            except UnicodeError:
                raise UnsafeArchiveError("Nome RAR não é UTF-8 válido.") from None
        _member_name(info.filename)
        budget.check_size(info.file_size)
        declared += info.file_size
        if budget.expanded + declared > budget.limits.max_expanded_bytes:
            raise ExtractionLimitError("O diretório RAR excede max_expanded_bytes.")

    names, count, total = _Names(), 0, 0
    try:
        with rarfile.RarFile(path, info_callback=inspect_header, errors="strict") as archive:
            if archive.needs_password():
                raise UnsupportedArchive("RAR criptografado requer senha; entrada não importada.")
            if len(archive.volumelist()) != 1:
                raise UnsupportedArchive("RAR multipartes não é suportado nesta versão.")
            for member in archive.infolist():
                if (member.is_symlink() or getattr(member, "file_redir", None)
                        or not (member.is_dir() or member.is_file())):
                    raise UnsafeArchiveError("Links e arquivos especiais RAR não são permitidos.")
                if getattr(member, "host_os", None) == getattr(rarfile, "RAR_OS_UNIX", 3):
                    if stat.S_IFMT(member.mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise UnsafeArchiveError("Dispositivo ou arquivo especial RAR não é permitido.")
                if member.is_dir():
                    total += _write_entry(index, files, names, member.filename, member.file_size, True, None, budget)
                else:
                    with archive.open(member) as stream:
                        total += _write_entry(index, files, names, member.filename, member.file_size, False, stream, budget)
                count += 1
    except ArchiveError:
        raise
    except rarfile.Error:
        raise ArchiveError("RAR inválido, incompleto, criptografado ou não suportado pelo backend.") from None
    return count, total


def _remove_stage(stage: Path, expanded: Path) -> None:
    if (stage.is_symlink() or stage.parent.resolve() != expanded.resolve()
            or not stage.name.startswith(".extract-") or not stage.resolve().is_relative_to(expanded.resolve())):
        raise UnsafeArchiveError("Diretório temporário de extração inválido.")
    shutil.rmtree(stage)


def _prepare_cache(path: Path, kind: str, expanded: Path, budget: _Budget) -> tuple[Path, str, bool]:
    def identity() -> tuple[int, int, int, int]:
        metadata = _regular_file(path)
        return metadata.st_size, metadata.st_mtime_ns, metadata.st_ino, metadata.st_dev

    before = identity()
    digest = _hash_file(path)
    if identity() != before:
        raise ArchiveError("O arquivo compactado mudou durante a leitura; feche a origem e tente novamente.")
    destination = expanded / digest
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise UnsafeArchiveError("Cache de extração inválido.")
        return destination, digest, False
    stage = expanded / f".extract-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    files = stage / "files"
    files.mkdir()
    try:
        with (stage / "members.jsonl").open("x", encoding="utf-8", newline="\n") as index:
            extractor = {"zip": _extract_zip, "tar": _extract_tar, "rar": _extract_rar}[kind]
            count, total = extractor(path, files, index, budget)
        if identity() != before:
            raise ArchiveError("O arquivo compactado mudou durante a extração; feche a origem e tente novamente.")
        with (stage / "complete.json").open("x", encoding="utf-8") as marker:
            json.dump({"schema": 1, "sha256": digest, "kind": kind, "members": count, "bytes": total}, marker)
            marker.flush()
            os.fsync(marker.fileno())
        try:
            os.rename(stage, destination)
        except FileExistsError:
            # Another scanner may have published the same content-addressed cache.
            pass
        return destination, digest, True
    except ArchiveError:
        raise
    except (OSError, EOFError, RuntimeError, UnicodeError, zipfile.BadZipFile, tarfile.TarError, NotImplementedError):
        raise ArchiveError("Não foi possível expandir o arquivo; verifique formato, integridade e espaço.") from None
    finally:
        if stage.exists():
            _remove_stage(stage, expanded)


def _cached_members(cache: Path, digest: str, kind: str, budget: _Budget,
                    already_accounted: bool) -> Iterator[tuple[Path, str]]:
    marker_path, index_path, files = cache / "complete.json", cache / "members.jsonl", cache / "files"
    try:
        _regular_file(marker_path)
        _regular_file(index_path)
    except OSError:
        raise ArchiveError("Cache incompleto; marcador ou índice não está disponível.") from None
    if files.is_symlink() or not files.is_dir():
        raise UnsafeArchiveError("Diretório de cache inválido.")
    with marker_path.open("rb") as stream:
        marker_bytes = stream.read(4097)
    if len(marker_bytes) > 4096:
        raise ArchiveError("Marcador de cache inválido.")
    try:
        marker = json.loads(marker_bytes)
    except (ValueError, UnicodeError):
        raise ArchiveError("Marcador de cache inválido.") from None
    if (not isinstance(marker, dict) or marker.get("schema") != 1 or marker.get("sha256") != digest
            or marker.get("kind") != kind):
        raise ArchiveError("Cache incompleto ou incompatível; não foi reutilizado.")
    for field in ("members", "bytes"):
        value = marker.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArchiveError("Contagens de cache inválidas.")
    if not already_accounted:
        if marker["members"] + budget.members > budget.limits.max_members:
            raise ExtractionLimitError("O cache excede max_members.")
        if marker["bytes"] + budget.expanded > budget.limits.max_expanded_bytes:
            raise ExtractionLimitError("O cache excede max_expanded_bytes.")
    names, count, total = _Names(), 0, 0
    with index_path.open("rb") as index:
        while True:
            line = index.readline(MAX_INDEX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_INDEX_LINE_BYTES:
                raise ArchiveError("Linha de índice de cache inválida.")
            try:
                entry = json.loads(line)
                name = _member_name(entry["name"])
                size, is_dir = entry["size"], entry["directory"]
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise ArchiveError("Índice de cache inválido.") from None
            if not isinstance(is_dir, bool) or isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ArchiveError("Índice de cache inválido.")
            names.add(name, is_dir)
            count += 1
            total += size
            if not already_accounted:
                budget.member(size)
                budget.expanded += size
            target = files.joinpath(*name.split("/"))
            # Inspect every parent so an externally introduced link cannot escape the cache.
            for current in (target, *target.parents):
                if current == cache:
                    break
                if current.is_symlink():
                    raise UnsafeArchiveError("Link encontrado no cache de extração.")
            if is_dir:
                if size or not target.is_dir():
                    raise ArchiveError("Diretório de cache divergente.")
            else:
                if _regular_file(target).st_size != size:
                    raise ArchiveError("Tamanho de arquivo em cache divergente.")
                yield target, name
    if marker.get("members") != count or marker.get("bytes") != total:
        raise ArchiveError("Contagem de cache divergente.")


def discover_files(root: Path, workspace: Path, limits: ExtractionLimits | None = None,
                   excluded_paths: Iterable[Path] = ()) -> list[DiscoveredFile]:
    """Find inputs recursively without modifying originals or following links.

    ``logical_parent`` omits archive wrappers but retains real/internal folders.
    Relative exclusions are relative to ``root``. They exclude that path/tree,
    whereas .git/.venv/__pycache__ and the workspace are always skipped.
    """
    root, workspace = Path(root).absolute(), Path(workspace).absolute()
    if root.is_symlink() or not root.is_dir():
        raise UnsafeArchiveError("A origem deve ser um diretório real, sem link simbólico.")
    root = root.resolve()
    for current in (workspace, *workspace.parents):
        if current.is_symlink():
            raise UnsafeArchiveError("O workspace não pode atravessar links simbólicos.")
    workspace = workspace.resolve()
    if root == workspace or root.is_relative_to(workspace):
        raise UnsafeArchiveError("O workspace deve ser separado da origem, sem contê-la.")
    excluded = [workspace]
    for path in excluded_paths:
        resolved = (root / path if not Path(path).is_absolute() else Path(path)).resolve()
        if root == resolved or root.is_relative_to(resolved):
            raise UnsafeArchiveError("Uma exclusão não pode excluir a própria origem.")
        excluded.append(resolved)
    workspace.mkdir(parents=True, exist_ok=True)
    expanded = workspace / "expanded"
    if expanded.is_symlink():
        raise UnsafeArchiveError("O diretório de expansão não pode ser link simbólico.")
    expanded.mkdir(mode=0o700, exist_ok=True)
    budget, result = _Budget(limits or ExtractionLimits()), []

    def source_path(value: str) -> str:
        # Escape delimiters before joining archive layers, preventing source-ID collisions.
        return value.replace("%", "%25").replace("!", "%21")

    def visit(path: Path, logical_parent: str, source: str, depth: int) -> None:
        archive_kind = _kind(path)
        if archive_kind:
            if depth >= budget.limits.max_depth:
                raise ExtractionLimitError("O aninhamento excede max_depth.")
            cache, digest, created = _prepare_cache(path, archive_kind, expanded, budget)
            for member_path, name in _cached_members(cache, digest, archive_kind, budget, created):
                parts = PurePosixPath(name).parts
                if any(part.casefold() in IGNORED_DIRECTORIES for part in parts[:-1]):
                    continue
                parent = "/".join(part for part in (logical_parent, *parts[:-1]) if part)
                visit(member_path, parent, f"{source}!{source_path(name)}", depth + 1)
        elif path.suffix.casefold() in SUPPORTED_FILES:
            _regular_file(path)
            result.append(DiscoveredFile(path, logical_parent, source))

    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name, reverse=True)
        for entry in entries:
            path = Path(entry.path)
            if any(path == excluded_path or path.is_relative_to(excluded_path) for excluded_path in excluded):
                continue
            if entry.is_symlink():
                raise UnsafeArchiveError("Link simbólico na origem não é percorrido.")
            relative = path.relative_to(root)
            try:
                relative.as_posix().encode("utf-8", errors="strict")
            except UnicodeError:
                raise UnsafeArchiveError("Caminho de entrada não é UTF-8 válido.") from None
            if entry.is_dir(follow_symlinks=False):
                if entry.name.casefold() not in IGNORED_DIRECTORIES:
                    pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                visit(path, "" if relative.parent == Path(".") else relative.parent.as_posix(), source_path(relative.as_posix()), 0)
    return sorted(result, key=lambda entry: entry.source)
