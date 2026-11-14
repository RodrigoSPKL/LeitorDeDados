"""Offline, append-only catalog of real TEXT tables grouped by folder and schema.

Identifier columns are discovered or configured; duplicate business records are
always retained. The import ledger skips an unchanged source, not duplicate rows.
SQLite BLOB values become a JSON string {"$binary":"base64","data":"..."}.
"""

from __future__ import annotations

import base64
from contextlib import closing, contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Callable, Iterator, Protocol
import unicodedata


MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_COLUMNS = 256
MAX_COLUMN_LENGTH = 256
_CSV_LOCK = threading.Lock()
_ALIASES = {
    "cpf": ("cpf", "documento", "documento_cpf", "nr_cpf", "numero_cpf", "cpf_cliente", "cpf_usuario"),
    "email": ("email", "e_mail", "e-mail", "mail", "email_cliente"),
    "phone": ("phone", "telefone", "celular", "fone", "numero_telefone", "telefone_cliente"),
    "code": ("code", "codigo", "id", "cod", "cod_cliente", "id_cliente"),
}


class DiscoveredFileLike(Protocol):
    path: Path
    logical_parent: str
    source: str


class CatalogError(ValueError):
    """Safe import errors omit source values, file paths and SQL statements."""


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _column_name(value: str) -> str:
    if not isinstance(value, str):
        raise CatalogError("Nome de coluna inválido.")
    result = unicodedata.normalize("NFKC", value).strip().casefold()
    if (not result or len(result) > MAX_COLUMN_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in result)):
        raise CatalogError("Nome de coluna vazio, inválido ou muito longo.")
    return result


def _columns(values: list[str]) -> list[str]:
    if not 1 <= len(values) <= MAX_COLUMNS:
        raise CatalogError("A origem deve ter entre uma e 256 colunas.")
    result = [_column_name(value) for value in values]
    if len(set(result)) != len(result):
        raise CatalogError("Cabeçalhos repetidos após normalizar os nomes.")
    return result


def _options(options: dict | None) -> dict:
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - {"encoding", "delimiter", "fields", "batch_size", "page_size"}:
        raise CatalogError("Opção de catálogo desconhecida.")
    result = {"encoding": options.get("encoding", "utf-8-sig"),
              "delimiter": options.get("delimiter"), "fields": options.get("fields", {}),
              "batch_size": options.get("batch_size", 1000), "page_size": options.get("page_size")}
    page_size = result["page_size"]
    if page_size is not None and (type(page_size) is not int or not 512 <= page_size <= 65536
                                  or page_size & (page_size - 1)):
        raise CatalogError("page_size deve ser uma potência de dois entre 512 e 65536.")
    if not isinstance(result["encoding"], str) or not result["encoding"]:
        raise CatalogError("Codificação inválida.")
    try:
        "".encode(result["encoding"])
    except (LookupError, TypeError):
        raise CatalogError("Codificação desconhecida.") from None
    delimiter = result["delimiter"]
    if delimiter is not None and (not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n\0"'):
        raise CatalogError("Separador inválido.")
    fields = result["fields"]
    if not isinstance(fields, dict) or set(fields) - set(_ALIASES):
        raise CatalogError("Mapeamento de identificadores inválido.")
    result["fields"] = {kind: _column_name(column) for kind, column in fields.items()}
    batch_size = result["batch_size"]
    if type(batch_size) is not int or not 1 <= batch_size <= 10_000:
        raise CatalogError("Lote deve conter entre um e 10 mil registros.")
    return result


def _fields(columns: list[str], overrides: dict) -> dict:
    present = set(columns)
    result = {}
    for kind, aliases in _ALIASES.items():
        if kind in overrides:
            if overrides[kind] in present:
                result[kind] = overrides[kind]
        else:
            for alias in aliases:
                if alias in present:
                    result[kind] = alias
                    break
    return result


def _table_name(logical_parent: str, source_table: str | None, columns: list[str]) -> str:
    group = json.dumps([logical_parent, source_table, columns], ensure_ascii=False, separators=(",", ":"))
    suffix = hashlib.sha256(group.encode("utf-8")).hexdigest()[:20]
    slug = unicodedata.normalize("NFKD", logical_parent).encode("ascii", errors="ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_").lower()[-32:] or "root"
    return f"t_{slug}_{suffix}"


def _stat_signature(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\0"


def _check_offline_sqlite(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(20)
    if (header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x01\x01"
            or any(Path(str(path) + ending).exists() or Path(str(path) + ending).is_symlink()
                   for ending in ("-wal", "-journal"))):
        raise CatalogError("Banco de origem precisa estar fechado e consolidado (checkpoint/DELETE) antes da importação.")


def _kind(path: Path) -> str:
    if _is_sqlite(path):
        _check_offline_sqlite(path)
        return "sqlite"
    suffix = path.suffix.casefold()
    if suffix in {".csv", ".tsv", ".txt"}:
        return "delimited"
    if suffix in {".jsonl", ".ndjson"}:
        return "jsonl"
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        raise CatalogError("Arquivo de banco não contém um banco SQLite válido.")
    raise CatalogError("Formato de origem não suportado pelo catálogo.")


class _LimitedLines:
    def __init__(self, stream):
        self.stream = stream
        self.record_bytes = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(MAX_ROW_BYTES + 1)
        if not line:
            raise StopIteration
        self.record_bytes += len(line.encode("utf-8"))
        if self.record_bytes > MAX_ROW_BYTES:
            raise CatalogError("Registro de origem excede 4 MiB.")
        return line


def _header_delimiter(stream, default: str) -> str:
    """Infer a fallback from parsed logical headers, respecting quoted text."""
    widths = {}
    for delimiter in (",", ";", "\t", "|"):
        stream.seek(0)
        try:
            header = next(csv.reader(_LimitedLines(stream), delimiter=delimiter, strict=True), None)
        except csv.Error:
            continue
        if header is not None:
            widths[delimiter] = len(header)
    stream.seek(0)
    split = {delimiter: width for delimiter, width in widths.items() if width > 1}
    if not split:
        if not widths:
            raise CatalogError("Cabeçalho inválido ou incompleto.")
        return default
    maximum = max(split.values())
    candidates = [delimiter for delimiter, width in split.items() if width == maximum]
    if len(candidates) != 1:
        raise CatalogError("Separador ambíguo no cabeçalho; configure delimiter explicitamente.")
    return candidates[0]


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Objeto JSON contém chaves repetidas.")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise CatalogError("Número JSON inválido.")


def _json_objects(stream) -> Iterator[dict]:
    while True:
        line = stream.readline(MAX_ROW_BYTES + 1)
        if not line:
            return
        if len(line.encode("utf-8")) > MAX_ROW_BYTES:
            raise CatalogError("Registro de origem excede 4 MiB.")
        if not line.strip():
            continue
        row = json.loads(line, object_pairs_hook=_json_pairs, parse_constant=_invalid_constant)
        if not isinstance(row, dict):
            raise CatalogError("JSONL deve conter um objeto por linha.")
        yield row


def _text(value) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return json.dumps({"$binary": "base64", "data": base64.b64encode(value).decode("ascii")}, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@contextmanager
def _source_groups(path: Path, kind: str, options: dict):
    if kind == "sqlite":
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None)) as connection:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_ROW_BYTES)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            tables = sorted(row[1] for row in connection.execute("PRAGMA main.table_list")
                            if row[2] == "table" and not row[1].startswith(("sqlite_", "_ld_")))
            canonical_tables = [_column_name(table) for table in tables]
            if len(set(canonical_tables)) != len(canonical_tables):
                raise CatalogError("Tabelas da origem têm nomes equivalentes após normalização.")

            def groups():
                for table in tables:
                    cursor = connection.execute(f"SELECT * FROM {_quoted(table)}")
                    header = _columns([description[0] for description in cursor.description])
                    yield _column_name(table), header, ([_text(value) for value in row] for row in cursor)

            try:
                yield groups()
            finally:
                connection.rollback()
    else:
        with path.open("r", encoding=options["encoding"], newline="") as stream:
            if kind == "jsonl":
                objects = _json_objects(stream)
                first = next(objects, None)
                if first is None:
                    yield iter(())
                    return
                header = _columns(list(first))

                def converted(row):
                    current_header = _columns(list(row))
                    if set(current_header) != set(header):
                        raise CatalogError("As linhas JSONL devem ter o mesmo conjunto de colunas.")
                    normalized = dict(zip(current_header, row.values()))
                    return [_text(normalized[column]) for column in header]

                def json_rows():
                    yield converted(first)
                    for row in objects:
                        yield converted(row)

                yield iter(((None, header, json_rows()),))
            else:
                sample = stream.read(4096)
                stream.seek(0)
                with _CSV_LOCK:
                    old_limit = csv.field_size_limit()
                    csv.field_size_limit(MAX_ROW_BYTES)
                    try:
                        delimiter = options["delimiter"]
                        if delimiter is None:
                            try:
                                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                            except csv.Error:
                                delimiter = _header_delimiter(stream, "\t" if path.suffix.casefold() == ".tsv" else ",")
                        lines = _LimitedLines(stream)
                        reader = csv.reader(lines, delimiter=delimiter, strict=True)
                        raw_header = next(reader, None)
                        if raw_header is None:
                            raise CatalogError("Arquivo delimitado sem cabeçalho.")
                        header = _columns(raw_header)

                        def csv_rows():
                            while True:
                                lines.record_bytes = 0
                                row = next(reader, None)
                                if row is None:
                                    return
                                if not row:
                                    continue
                                if len(row) != len(header):
                                    raise CatalogError("Quantidade de colunas diferente do cabeçalho.")
                                yield row

                        yield iter(((None, header, csv_rows()),))
                    finally:
                        csv.field_size_limit(old_limit)


def _open_catalog(path: Path, page_size: int | None) -> sqlite3.Connection:
    if path.is_symlink():
        raise CatalogError("O destino do catálogo não pode ser um link simbólico.")
    existing = path.exists() and path.stat().st_size > 0
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        if existing:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if not {"_ld_meta", "_ld_tables", "_ld_imports"}.issubset(names):
                raise CatalogError("O destino existente não é um catálogo gerenciado deste projeto.")
            if connection.execute("SELECT value FROM _ld_meta WHERE key='schema_version'").fetchone() != ("1",):
                raise CatalogError("Versão de catálogo incompatível.")
            actual_page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            if page_size is not None and page_size != actual_page_size:
                raise CatalogError("page_size difere do catálogo existente; use o tamanho original, sem conversão automática.")
        else:
            connection.execute(f"PRAGMA page_size={page_size or 16384}")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS _ld_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID")
        connection.execute("INSERT OR IGNORE INTO _ld_meta(key,value) VALUES ('schema_version','1')")
        connection.execute("CREATE TABLE IF NOT EXISTS _ld_tables("
                           "name TEXT PRIMARY KEY,logical_parent TEXT NOT NULL,source_table TEXT,"
                           "schema_json TEXT NOT NULL,fields_json TEXT NOT NULL,row_count INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID")
        connection.execute("CREATE TABLE IF NOT EXISTS _ld_imports("
                           "source TEXT NOT NULL,sha256 TEXT NOT NULL,target_table TEXT NOT NULL,"
                           "rows INTEGER NOT NULL,imported_at TEXT NOT NULL,"
                           "PRIMARY KEY(source,sha256,target_table)) WITHOUT ROWID")
        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def _ensure_table(connection: sqlite3.Connection, logical_parent: str, source_table: str | None,
                  columns: list[str], fields: dict) -> str:
    name = _table_name(logical_parent, source_table, columns)
    schema_json = json.dumps(columns, ensure_ascii=False, separators=(",", ":"))
    fields_json = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = connection.execute("SELECT logical_parent,source_table,schema_json FROM _ld_tables WHERE name=?", (name,)).fetchone()
    if existing is None:
        statement = ",".join(f"{_quoted(column)} TEXT" for column in columns)
        connection.execute(f"CREATE TABLE {_quoted(name)}({statement})")
        connection.execute("INSERT INTO _ld_tables(name,logical_parent,source_table,schema_json,fields_json) VALUES (?,?,?,?,?)",
                           (name, logical_parent, source_table, schema_json, fields_json))
    elif existing != (logical_parent, source_table, schema_json):
        raise CatalogError("Conflito na identidade da tabela do catálogo.")
    else:
        connection.execute("UPDATE _ld_tables SET fields_json=? WHERE name=?", (fields_json, name))
    return name


def _import_group(connection: sqlite3.Connection, logical_parent: str, source_table: str | None,
                  original_columns: list[str], rows: Iterator[list], source: str, digest: str,
                  options: dict) -> tuple[int, bool]:
    columns = sorted(original_columns)
    fields = _fields(columns, options["fields"])
    name = _ensure_table(connection, logical_parent, source_table, columns, fields)
    if connection.execute("SELECT 1 FROM _ld_imports WHERE source=? AND sha256=? AND target_table=?",
                          (source, digest, name)).fetchone() is not None:
        return 0, False
    positions = [original_columns.index(column) for column in columns]
    statement = f"INSERT INTO {_quoted(name)} ({','.join(map(_quoted, columns))}) VALUES ({','.join('?' for _ in columns)})"
    batch = []
    batch_bytes = 0
    count = 0
    for row in rows:
        if len(row) != len(original_columns):
            raise CatalogError("Quantidade de valores diferente do esquema da origem.")
        values = tuple(row[position] for position in positions)
        size = sum(len(value.encode("utf-8")) if value is not None else 0 for value in values)
        if size > MAX_ROW_BYTES:
            raise CatalogError("Registro convertido excede 4 MiB.")
        if batch and batch_bytes + size > 64 * 1024 * 1024:
            connection.executemany(statement, batch)
            batch.clear()
            batch_bytes = 0
        batch.append(values)
        batch_bytes += size
        count += 1
        if len(batch) >= options["batch_size"]:
            connection.executemany(statement, batch)
            batch.clear()
            batch_bytes = 0
    if batch:
        connection.executemany(statement, batch)
    connection.execute("UPDATE _ld_tables SET row_count=row_count+? WHERE name=?", (count, name))
    connection.execute("INSERT INTO _ld_imports(source,sha256,target_table,rows,imported_at) VALUES (?,?,?,?,?)",
                       (source, digest, name, count, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
    return count, True


def _report_tables(connection: sqlite3.Connection) -> list[dict]:
    return [{"name": name, "logical_parent": logical_parent, "source_table": source_table,
             "columns": json.loads(schema), "rows": row_count, "fields": json.loads(fields)}
            for name, logical_parent, source_table, schema, fields, row_count in connection.execute(
                "SELECT name,logical_parent,source_table,schema_json,fields_json,row_count FROM _ld_tables "
                "ORDER BY logical_parent,source_table,name")]


def import_files(files: list[DiscoveredFileLike], catalog_path: Path, options: dict | None = None,
                 progress: Callable[[int], None] | None = None) -> dict:
    """Append each source transactionally; unchanged source/hash/target skips.

    Changed files append in full. Files with distinct source identities append
    even if their contents are identical. A failed source rolls back all tables
    contributed by that source; previously committed sources remain resumable.
    Progress receives the newly committed row count after each imported file.
    """
    options = _options(options)
    if not isinstance(files, list) or (progress is not None and not callable(progress)):
        raise CatalogError("Lista de origens ou callback de progresso inválido.")
    catalog_path = Path(catalog_path).absolute()
    imported_files = skipped_files = imported_rows = 0
    try:
        with closing(_open_catalog(catalog_path, options["page_size"])) as connection:
            for item in files:
                if (not isinstance(getattr(item, "source", None), str) or not item.source
                        or not isinstance(getattr(item, "logical_parent", None), str)):
                    raise CatalogError("Metadados de origem inválidos.")
                path = Path(item.path)
                if path.resolve() == catalog_path.resolve() or os.path.samefile(path, catalog_path):
                    raise CatalogError("O catálogo não pode ser importado como sua própria origem.")
                before = _stat_signature(path)
                kind = _kind(path)
                digest = _hash_file(path)
                if _stat_signature(path) != before:
                    raise CatalogError("A origem mudou durante a leitura; importe apenas arquivos fechados.")
                connection.execute("BEGIN IMMEDIATE")
                file_rows = 0
                any_imported = False
                try:
                    with _source_groups(path, kind, options) as groups:
                        for source_table, columns, rows in groups:
                            count, imported = _import_group(connection, item.logical_parent, source_table,
                                                             columns, rows, item.source, digest, options)
                            file_rows += count
                            any_imported |= imported
                    if kind == "sqlite":
                        _check_offline_sqlite(path)
                    if _stat_signature(path) != before:
                        raise CatalogError("A origem mudou durante a leitura; importe apenas arquivos fechados.")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                if any_imported:
                    imported_files += 1
                    imported_rows += file_rows
                    if progress is not None:
                        progress(imported_rows)
                else:
                    skipped_files += 1
            return {"tables": _report_tables(connection), "imported_files": imported_files,
                    "skipped_files": skipped_files, "imported_rows": imported_rows,
                    "catalog_page_size": connection.execute("PRAGMA page_size").fetchone()[0]}
    except CatalogError:
        raise
    except (OSError, sqlite3.Error, UnicodeError, csv.Error, ValueError, TypeError, RecursionError):
        raise CatalogError("Falha ao importar origem; confira formato, codificação, esquema e permissões.") from None
