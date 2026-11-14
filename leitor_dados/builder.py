"""Streaming CSV/JSONL ingestion into a versioned, immutable SQLite dataset."""

from __future__ import annotations

from collections import defaultdict
import base64
from contextlib import closing
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from typing import Callable, Iterator
import uuid

from .normalization import KINDS, data_shard, lookup_shard, normalize, normalization_settings


SCHEMA_VERSION = 1
MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_TOTAL_SHARDS = 128
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_CSV_LIMIT_LOCK = threading.Lock()


class BuildError(ValueError):
    """Import failure safe to display without including source data."""


def _integer(value: object, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BuildError(f"Configuração inválida: {label}.")
    return value


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise BuildError("Configuração deve ser um objeto JSON.")
    allowed = {"format", "csv_delimiter", "encoding", "fields", "normalization",
               "data_shards", "lookup_shards", "batch_size", "sqlite_table", "metadata", "invalid_identifiers"}
    if set(config) - allowed:
        raise BuildError("Configuração contém opção desconhecida.")
    result = {
        "format": config.get("format", "csv"),
        "csv_delimiter": config.get("csv_delimiter", ","),
        "encoding": config.get("encoding", "utf-8-sig"),
        "fields": config.get("fields"),
        "normalization": normalization_settings(config.get("normalization")),
        "data_shards": _integer(config.get("data_shards", 8), 1, MAX_TOTAL_SHARDS, "data_shards"),
        "lookup_shards": _integer(config.get("lookup_shards", 16), 1, MAX_TOTAL_SHARDS, "lookup_shards"),
        "batch_size": _integer(config.get("batch_size", 1000), 1, 10_000, "batch_size"),
        "sqlite_table": config.get("sqlite_table"),
        "metadata": config.get("metadata"),
        "invalid_identifiers": config.get("invalid_identifiers", "error"),
    }
    if result["format"] not in ("csv", "jsonl", "sqlite"):
        raise BuildError("Formato deve ser csv, jsonl ou sqlite.")
    if result["format"] == "sqlite" and (not isinstance(result["sqlite_table"], str)
            or not result["sqlite_table"] or "\0" in result["sqlite_table"]):
        raise BuildError("Informe a tabela SQLite de origem.")
    if result["metadata"] is not None and (not isinstance(result["metadata"], dict)
            or any(not isinstance(k, str) or not isinstance(v, (str, type(None)))
                   for k, v in result["metadata"].items())):
        raise BuildError("Metadados de origem inválidos.")
    if result["invalid_identifiers"] not in ("error", "skip"):
        raise BuildError("Política de identificadores inválida.")
    delimiter = result["csv_delimiter"]
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\r\n\0\"":
        raise BuildError("Separador CSV inválido.")
    encoding = result["encoding"]
    if not isinstance(encoding, str) or not encoding:
        raise BuildError("Codificação inválida.")
    try:
        "".encode(encoding)
    except (LookupError, TypeError):
        raise BuildError("Codificação desconhecida.") from None
    fields = result["fields"]
    if (not isinstance(fields, dict) or set(fields) - set(KINDS)
            or any(not isinstance(value, str) or not value.strip() for value in fields.values())):
        raise BuildError("Mapeamento de identificadores inválido.")
    result["fields"] = dict(fields)
    if result["data_shards"] + result["lookup_shards"] > MAX_TOTAL_SHARDS:
        raise BuildError("O total de shards não pode exceder 128.")
    return result


def _version_name(version: str) -> None:
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise BuildError("Nome de versão inválido.")


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise BuildError("Links simbólicos não são aceitos no dataset.")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o644)


@contextmanager
def _version_reservation(versions: Path, version: str):
    """Cooperating builders never replace an already built version."""
    lock = versions / f".reserve-{version}"
    try:
        lock.mkdir()
    except FileExistsError:
        raise BuildError("A versão está sendo construída por outro processo.") from None
    try:
        if (versions / version).exists() or (versions / version).is_symlink():
            raise BuildError("A versão já existe; escolha um novo nome.")
        yield
    finally:
        lock.rmdir()


class _LimitedCsvLines:
    def __init__(self, stream):
        self.stream = stream
        self.bytes_in_record = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(MAX_ROW_BYTES + 1)
        if not line:
            raise StopIteration
        self.bytes_in_record += len(line.encode("utf-8"))
        if self.bytes_in_record > MAX_ROW_BYTES:
            raise BuildError("Registro de entrada excede o limite de 4 MiB.")
        return line


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Chave JSON repetida.")
        result[key] = value
    return result


def _invalid_json_constant(_value: str):
    raise ValueError("Número JSON inválido.")


def _sqlite_input(path: Path, config: dict, input_index: int) -> Iterator[tuple[int, dict]]:
    with path.open("rb") as stream:
        header = stream.read(100)
    if not header.startswith(b"SQLite format 3\0"):
        raise BuildError("A origem DB não é um banco SQLite compatível.")
    if len(header) < 20 or header[18:20] != b"\x01\x01" or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise BuildError("Banco de origem deve estar fechado e consolidado em modo DELETE.")
    before = path.stat()
    table = config["sqlite_table"]
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_ROW_BYTES)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        sql = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
        if table.startswith("sqlite_") or not sql or not sql[0] or re.search(r"\bCREATE\s+VIRTUAL\s+TABLE\b", sql[0], re.IGNORECASE):
            raise BuildError("Tabela SQLite de origem inválida.")
        quoted = '"' + table.replace('"', '""') + '"'
        cursor = connection.execute(f"SELECT * FROM {quoted}")
        columns = [item[0] for item in cursor.description]
        if not set(config["fields"].values()).issubset(columns):
            raise BuildError("Coluna mapeada ausente na origem SQLite.")
        for number, values in enumerate(cursor, 1):
            row = {}
            for column, value in zip(columns, values):
                if isinstance(value, bytes):
                    value = json.dumps({"$binary": "base64", "data": base64.b64encode(value).decode("ascii")})
                elif value is not None and not isinstance(value, str):
                    value = str(value)
                row[column] = value
            yield number, row
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise BuildError("Banco de origem foi alterado durante a leitura.")


def _iter_input(path: Path, config: dict, input_index: int) -> Iterator[tuple[int, dict]]:
    try:
        if config["format"] == "sqlite":
            yield from _sqlite_input(path, config, input_index)
            return
        with path.open("r", encoding=config["encoding"], newline="") as stream:
            if config["format"] == "jsonl":
                line_number = 0
                while True:
                    line = stream.readline(MAX_ROW_BYTES + 1)
                    if not line:
                        break
                    line_number += 1
                    if len(line.encode("utf-8")) > MAX_ROW_BYTES:
                        raise BuildError("Registro de entrada excede o limite de 4 MiB.")
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line, object_pairs_hook=_no_duplicate_keys,
                                         parse_constant=_invalid_json_constant)
                    except (ValueError, RecursionError):
                        raise BuildError(f"JSON inválido na entrada {input_index}, linha {line_number}.") from None
                    if not isinstance(row, dict):
                        raise BuildError(f"Registro deve ser objeto na entrada {input_index}, linha {line_number}.")
                    yield line_number, row
            else:
                with _CSV_LIMIT_LOCK:
                    old_limit = csv.field_size_limit()
                    csv.field_size_limit(MAX_ROW_BYTES)
                    try:
                        lines = _LimitedCsvLines(stream)
                        reader = csv.reader(lines, delimiter=config["csv_delimiter"], strict=True)
                        header = next(reader, None)
                        if header is None:
                            raise BuildError(f"CSV sem cabeçalho na entrada {input_index}.")
                        if not header or any(not name for name in header) or len(set(header)) != len(header):
                            raise BuildError(f"Cabeçalho CSV inválido na entrada {input_index}.")
                        if not set(config["fields"].values()).issubset(header):
                            raise BuildError(f"Coluna mapeada ausente na entrada {input_index}.")
                        record_number = 0
                        while True:
                            lines.bytes_in_record = 0
                            values = next(reader, None)
                            if values is None:
                                break
                            if not values:
                                continue
                            record_number += 1
                            if len(values) != len(header):
                                raise BuildError(f"Quantidade de colunas inválida na entrada {input_index}, registro {record_number}.")
                            yield record_number, dict(zip(header, values))
                    finally:
                        csv.field_size_limit(old_limit)
    except BuildError:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError):
        raise BuildError(f"Não foi possível ler a entrada {input_index}; confira formato e codificação.") from None


def _open_shard(path: Path, lookup: bool) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA cache_size=-512")
        connection.execute("PRAGMA user_version=1")
        if lookup:
            connection.execute(
                "CREATE TABLE lookup(kind TEXT NOT NULL,value TEXT NOT NULL,record_id INTEGER NOT NULL,"
                "PRIMARY KEY(kind,value,record_id)) WITHOUT ROWID"
            )
        else:
            connection.execute("CREATE TABLE records(record_id INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def _file_info(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return {"name": path.name, "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def build_dataset(inputs: list[Path], config: dict, root: Path, version: str,
                  publish: bool = False, progress: Callable[[int], None] | None = None,
                  source_configs: list[dict] | None = None, dataset_metadata: dict | None = None) -> dict:
    """Build a complete version, then optionally atomically publish current.json.

    Existing versions are never updated. A failed import deletes its own staging
    directory, leaving the previously published version untouched. Record IDs
    are sequential within a version and must not be reused across versions.
    Per-source overrides may change only format, csv_delimiter, encoding and
    fields; routing and normalization remain global. Progress receives only the
    committed record count after each nonempty batch, before publication.
    """
    config = validate_config(config)
    if dataset_metadata is None:
        dataset_metadata = {}
    try:
        if not isinstance(dataset_metadata, dict) or len(json.dumps(dataset_metadata, ensure_ascii=False,
                allow_nan=False).encode("utf-8")) > 65536:
            raise ValueError()
    except (TypeError, ValueError, UnicodeError):
        raise BuildError("Metadados do acervo inválidos ou maiores que 64 KiB.") from None
    _version_name(version)
    if not isinstance(publish, bool):
        raise BuildError("publish deve ser booleano.")
    if progress is not None and not callable(progress):
        raise BuildError("progress deve ser uma função ou None.")
    if not isinstance(inputs, list) or not inputs:
        raise BuildError("Informe pelo menos uma entrada.")
    inputs = [Path(path) for path in inputs]
    try:
        resolved_inputs = [path.resolve() for path in inputs]
    except (OSError, RuntimeError):
        raise BuildError("Não foi possível resolver um caminho de entrada.") from None
    per_source_configs = [config] * len(inputs)
    if source_configs is not None:
        if not isinstance(source_configs, list) or len(source_configs) != len(inputs):
            raise BuildError("As configurações de origem devem corresponder às entradas.")
        allowed_source_options = {"format", "csv_delimiter", "encoding", "fields", "sqlite_table", "metadata"}
        per_source_configs = []
        for override in source_configs:
            if not isinstance(override, dict) or set(override) - allowed_source_options:
                raise BuildError("Origem contém opção não permitida; normalização e shards são globais.")
            per_source_configs.append(validate_config({**config, **override}))
    identities = [(path, source["sqlite_table"] if source["format"] == "sqlite" else None)
                  for path, source in zip(resolved_inputs, per_source_configs)]
    if len(set(identities)) != len(identities):
        raise BuildError("A mesma entrada foi informada mais de uma vez.")
    root = Path(root).absolute()
    _reject_symlink(root)
    root_existed = root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if not root_existed:
        root.chmod(0o755)
    versions = root / "versions"
    _reject_symlink(versions)
    versions_existed = versions.exists()
    versions.mkdir(exist_ok=True)
    if not versions_existed:
        versions.chmod(0o755)
    stage = root / f".build-{uuid.uuid4().hex}"
    connections: list[sqlite3.Connection] = []
    with _version_reservation(versions, version):
        stage.mkdir()
        stage.chmod(0o755)
        try:
            data_paths = [stage / f"records-{index:04d}.sqlite" for index in range(config["data_shards"])]
            lookup_paths = [stage / f"lookup-{index:04d}.sqlite" for index in range(config["lookup_shards"])]
            data_connections = []
            for path in data_paths:
                connection = _open_shard(path, lookup=False)
                connections.append(connection)
                data_connections.append(connection)
            lookup_connections = []
            for path in lookup_paths:
                connection = _open_shard(path, lookup=True)
                connections.append(connection)
                lookup_connections.append(connection)
            data_batches = defaultdict(list)
            lookup_batches = defaultdict(list)
            batch_rows = 0
            batch_bytes = 0

            def flush() -> None:
                nonlocal batch_rows, batch_bytes
                if not batch_rows:
                    return
                for shard, rows in data_batches.items():
                    data_connections[shard].executemany("INSERT INTO records(record_id,payload) VALUES (?,?)", rows)
                    data_connections[shard].commit()
                for shard, rows in lookup_batches.items():
                    lookup_connections[shard].executemany("INSERT INTO lookup(kind,value,record_id) VALUES (?,?,?)", rows)
                    lookup_connections[shard].commit()
                data_batches.clear()
                lookup_batches.clear()
                batch_rows = batch_bytes = 0
                if progress is not None:
                    progress(row_count)

            row_count = 0
            invalid_identifiers = {kind: 0 for kind in KINDS}
            for input_index, (path, source_config) in enumerate(zip(inputs, per_source_configs), start=1):
                for row_number, row in _iter_input(path, source_config, input_index):
                    try:
                        payload_row = {"_ld": source_config["metadata"], "record": row} if source_config["metadata"] is not None else row
                        payload = json.dumps(payload_row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                        payload_bytes = len(payload.encode("utf-8"))
                    except (ValueError, UnicodeError, RecursionError):
                        raise BuildError(f"Payload inválido na entrada {input_index}, registro {row_number}.") from None
                    if payload_bytes > MAX_ROW_BYTES:
                        raise BuildError(f"Payload excede 4 MiB na entrada {input_index}, registro {row_number}.")
                    identifiers = []
                    for kind, source in source_config["fields"].items():
                        try:
                            value = normalize(kind, row.get(source), config["normalization"])
                        except ValueError:
                            if config["invalid_identifiers"] == "skip":
                                invalid_identifiers[kind] += 1
                                continue
                            raise BuildError(f"Identificador inválido ({kind}) na entrada {input_index}, registro {row_number}.") from None
                        if value is not None:
                            identifiers.append((kind, value))
                    estimate_bytes = payload_bytes + sum(len(value.encode("utf-8")) + 64 for _, value in identifiers)
                    if batch_rows and batch_bytes + estimate_bytes > MAX_BATCH_BYTES:
                        flush()
                    row_count += 1
                    if row_count > 2**63 - 1:
                        raise BuildError("Quantidade de registros excede o limite de IDs.")
                    data_batches[data_shard(row_count, config["data_shards"])].append((row_count, payload))
                    for kind, value in identifiers:
                        shard = lookup_shard(kind, value, config["lookup_shards"])
                        lookup_batches[shard].append((kind, value, row_count))
                    batch_rows += 1
                    batch_bytes += estimate_bytes
                    if batch_rows >= config["batch_size"]:
                        flush()
            flush()
            for connection in connections:
                connection.execute("PRAGMA optimize")
                connection.commit()
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise BuildError("Falha na verificação SQLite da versão construída.")
                connection.close()
            connections.clear()
            for path in data_paths + lookup_paths:
                path.chmod(0o644)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "version": version,
                "data_shards": config["data_shards"],
                "lookup_shards": config["lookup_shards"],
                "row_count": row_count,
                "normalization": config["normalization"],
                "invalid_identifiers": invalid_identifiers,
                "dataset_metadata": dataset_metadata,
                "files": [_file_info(path) for path in data_paths + lookup_paths],
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            _write_json(stage / "manifest.json", manifest)
            _fsync_directory(stage)
            target = versions / version
            if target.exists() or target.is_symlink():
                raise BuildError("A versão já existe; escolha um novo nome.")
            os.replace(stage, target)
            _fsync_directory(versions)
            if publish:
                publish_version(root, version)
            return manifest
        except sqlite3.Error:
            raise BuildError("Falha ao construir os shards SQLite; a versão não foi publicada.") from None
        finally:
            for connection in connections:
                connection.close()
            if stage.exists():
                # The generated stage is a direct child of the checked root.
                if stage.parent != root or not stage.name.startswith(".build-") or stage.is_symlink():
                    raise BuildError("Diretório temporário de construção inválido.")
                shutil.rmtree(stage)


def publish_version(root: Path, version: str) -> None:
    """Atomically select a complete, existing version without touching its files."""
    _version_name(version)
    root = Path(root).absolute()
    versions = root / "versions"
    directory = versions / version
    manifest_path = directory / "manifest.json"
    for path in (root, versions, directory, manifest_path, root / "current.json"):
        _reject_symlink(path)
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("version") != version):
            raise BuildError("Manifesto da versão inválido.")
        data_count = _integer(manifest.get("data_shards"), 1, MAX_TOTAL_SHARDS, "data_shards")
        lookup_count = _integer(manifest.get("lookup_shards"), 1, MAX_TOTAL_SHARDS, "lookup_shards")
        if data_count + lookup_count > MAX_TOTAL_SHARDS:
            raise BuildError("Manifesto da versão inválido.")
        row_count = manifest.get("row_count")
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
            raise BuildError("Manifesto da versão inválido.")
        normalization_settings(manifest.get("normalization"))
        expected = {f"records-{index:04d}.sqlite" for index in range(data_count)}
        expected.update(f"lookup-{index:04d}.sqlite" for index in range(lookup_count))
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) != len(expected):
            raise BuildError("Manifesto da versão inválido.")
        seen = set()
        for item in files:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    or item["name"] not in expected or item["name"] in seen
                    or isinstance(item.get("bytes"), bool) or not isinstance(item.get("bytes"), int)
                    or item["bytes"] < 0 or not isinstance(item.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
                raise BuildError("Manifesto da versão inválido.")
            seen.add(item["name"])
            path = directory / item["name"]
            _reject_symlink(path)
            if not path.is_file() or path.stat().st_size != item["bytes"]:
                raise BuildError("Arquivo da versão ausente ou com tamanho divergente.")
    except BuildError:
        raise
    except (OSError, ValueError, TypeError):
        raise BuildError("Não foi possível validar o manifesto da versão.") from None
    temporary = root / f".current-{uuid.uuid4().hex}.json"
    try:
        _write_json(temporary, {"version": version})
        os.replace(temporary, root / "current.json")
        _fsync_directory(root)
    finally:
        if temporary.exists():
            temporary.unlink()
