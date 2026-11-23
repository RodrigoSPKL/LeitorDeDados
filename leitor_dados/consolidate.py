"""Offline, streaming full-record deduplication with verified, opt-in source removal."""
from __future__ import annotations

import base64
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .catalog import CatalogError, MAX_ROW_BYTES, _LimitedLines, _header_delimiter, _quoted


APPLICATION_ID = 0x4C444331
FORMATS = {".csv", ".tsv", ".txt", ".db", ".sqlite", ".sqlite3"}
LOCK = ".consolidar-sqlite.lock"


class ConsolidationError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _signature(path):
    value = path.lstat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def _link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _regular(path):
    if _link(path) or not stat.S_ISREG(path.lstat().st_mode) or path.lstat().st_nlink != 1:
        raise ConsolidationError("A origem deve ser um arquivo regular, sem links.")


def _fingerprint(path):
    _regular(path)
    before = _signature(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(block)
    if before != _signature(path):
        raise ConsolidationError("Um arquivo mudou durante a leitura; limpeza cancelada.")
    return before, digest.hexdigest()


def _inventory(root, excluded):
    files, folders = {}, {}
    device = root.stat().st_dev

    def visit(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if relative == LOCK:
                continue
            if relative in excluded:
                _regular(path)
                continue
            if _link(path):
                raise ConsolidationError(f"Link recusado: {relative!r}.")
            value = path.lstat()
            if value.st_dev != device or os.path.ismount(path):
                raise ConsolidationError(f"Ponto de montagem recusado: {relative!r}.")
            if stat.S_ISDIR(value.st_mode):
                if path.name.casefold() == "backup":
                    if directory != root or path.name != "backup":
                        raise ConsolidationError("Coloque os backups em uma única pasta 'backup' na raiz.")
                    continue
                if path.name == ".git":
                    raise ConsolidationError("Execute na pasta de dados, fora do repositório do projeto.")
                folders[relative] = [value.st_dev, value.st_ino]
                visit(path)
            elif stat.S_ISREG(value.st_mode) and path.suffix.casefold() in FORMATS:
                files[relative] = _signature(path)
            else:
                raise ConsolidationError(f"Arquivo/formato não suportado: {relative!r}; nada será limpo.")

    visit(root)
    return files, folders


def _check_outputs(final, pending):
    if final.exists() and pending.exists():
        raise ConsolidationError("Há banco final e provisório ao mesmo tempo; fontes preservadas.")
    for database in (final, pending):
        journal = Path(str(database) + "-journal")
        if journal.exists() or journal.is_symlink():
            _regular(journal)
            if not database.exists():
                raise ConsolidationError("Journal sem banco correspondente; fontes preservadas.")


def _header(columns):
    if not columns or len(columns) > 256 or any(not name or "\x00" in name for name in columns):
        raise ConsolidationError("Cabeçalho vazio, inválido ou com mais de 256 colunas.")
    if len(set(columns)) != len(columns):
        raise ConsolidationError("Cabeçalho contém nomes de colunas repetidos.")
    return columns


def _sqlite_value(value):
    if isinstance(value, bytes):
        return {"$sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
    return value


def _rows(path, encoding, delimiter):
    suffix = path.suffix.casefold()
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        with path.open("rb") as stream:
            header = stream.read(20)
        if header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x01\x01":
            raise ConsolidationError("A origem .db precisa ser SQLite fechado, consolidado em journal_mode=DELETE.")
        if any(Path(str(path) + ending).exists() for ending in ("-wal", "-shm", "-journal")):
            raise ConsolidationError("Feche e consolide o SQLite de origem antes de importar.")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            source.execute("PRAGMA trusted_schema=OFF")
            source.execute("PRAGMA query_only=ON")
            source.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_ROW_BYTES)
            source.execute("BEGIN")
            entries = list(source.execute("PRAGMA main.table_list"))
            if any(row[2] in {"virtual", "shadow", "view"} and not row[1].startswith("sqlite_")
                   for row in entries):
                raise ConsolidationError("SQLite com views ou tabelas virtuais não é suportado; exporte para CSV.")
            tables = sorted(row[1] for row in entries if row[2] == "table" and not row[1].startswith("sqlite_"))
            for table in tables:
                cursor = source.execute(f"SELECT * FROM {_quoted(table)}")
                columns = _header([item[0] for item in cursor.description])
                for row in cursor:
                    yield table, dict(zip(columns, map(_sqlite_value, row)))
        return
    with path.open("r", encoding=encoding, newline="") as stream:
        if suffix == ".txt":
            while True:
                line = stream.readline(MAX_ROW_BYTES + 1)
                if not line:
                    return
                if len(line.encode("utf-8")) > MAX_ROW_BYTES:
                    raise ConsolidationError("Linha TXT excede 4 MiB.")
                # Only the line terminator is removed; blank lines and spaces survive.
                text = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith(("\r", "\n")) else line
                yield None, {"texto": text}
        else:
            selected = delimiter
            if selected is None:
                sample = stream.read(4096)
                stream.seek(0)
                try:
                    selected = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                except csv.Error:
                    selected = _header_delimiter(stream, "\t" if suffix == ".tsv" else ",")
            old_limit = csv.field_size_limit()
            csv.field_size_limit(MAX_ROW_BYTES)
            try:
                limited = _LimitedLines(stream)
                reader = csv.reader(limited, delimiter=selected, strict=True)
                columns = _header(next(reader, []))
                while True:
                    limited.record_bytes = 0
                    row = next(reader, None)
                    if row is None:
                        return
                    if not row:
                        raise ConsolidationError(f"Linha CSV vazia na linha física {reader.line_num}; corrija a origem.")
                    if len(row) != len(columns):
                        raise ConsolidationError(f"CSV com {len(row)} campos; esperados {len(columns)}, linha física {reader.line_num}.")
                    yield None, dict(zip(columns, row))
            finally:
                csv.field_size_limit(old_limit)


def _connect(path, root, options, *, create):
    if not create:
        _regular(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if not create:
            if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ConsolidationError("O nome do banco de saída já pertence a outro arquivo; nada foi sobrescrito.")
            config = dict(connection.execute("SELECT chave, valor FROM configuracao"))
            if config.get("formato") != "1" or config.get("raiz") != str(root) or config.get("opcoes") != _json(options):
                raise ConsolidationError("Banco existente pertence a outra raiz ou configuração; use as opções originais.")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA cache_size=-65536")
        connection.execute("PRAGMA temp_store=FILE")
        if create:
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.executescript('''
                CREATE TABLE configuracao(chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
                CREATE TABLE fontes(id INTEGER PRIMARY KEY, caminho TEXT NOT NULL UNIQUE,
                    assinatura TEXT NOT NULL, sha256 TEXT NOT NULL, completo INTEGER NOT NULL DEFAULT 0,
                    linhas INTEGER NOT NULL DEFAULT 0, novos INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE pastas(caminho TEXT PRIMARY KEY, identidade TEXT NOT NULL);
                CREATE TABLE registros(hash BLOB PRIMARY KEY CHECK(length(hash)=32), dados TEXT NOT NULL,
                    fonte_id INTEGER NOT NULL, tabela_origem TEXT) WITHOUT ROWID;
            ''')
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany("INSERT INTO configuracao VALUES (?,?)", [
                ("formato", "1"), ("raiz", str(root)), ("opcoes", _json(options)), ("estado", "importando")])
            connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def _event(progress, stage, **values):
    if progress:
        progress({"stage": stage, **values})


def _import_source(connection, root, name, options, batch_size, progress):
    path = root / name
    _event(progress, "hashing_source", arquivo=name)
    signature, checksum = _fingerprint(path)
    saved = connection.execute("SELECT id, assinatura, sha256, completo, novos FROM fontes WHERE caminho=?", (name,)).fetchone()
    if saved:
        source_id, expected, digest, complete, new_count = saved
        if expected != _json(signature) or digest != checksum:
            raise ConsolidationError(f"Origem mudou desde a importação: {name!r}. Reconstrua em outro banco; fontes preservadas.")
        if complete:
            _event(progress, "source_already_imported", arquivo=name)
            return
    else:
        cursor = connection.execute("INSERT INTO fontes(caminho, assinatura, sha256) VALUES (?,?,?)", (name, _json(signature), checksum))
        source_id, new_count = cursor.lastrowid, 0
    rows_read, pending = 0, 0
    connection.execute("BEGIN IMMEDIATE")

    def commit_batch():
        connection.execute("UPDATE fontes SET linhas=?, novos=? WHERE id=?", (rows_read, new_count, source_id))
        connection.commit()
        _event(progress, "rows_committed", arquivo=name, linhas_lidas=rows_read, novos_acumulados=new_count)

    try:
        for table, row in _rows(path, options["encoding"], options["delimiter"]):
            payload = _json(row)
            encoded = payload.encode("utf-8")
            if len(encoded) > MAX_ROW_BYTES:
                raise ConsolidationError("Registro convertido excede 4 MiB.")
            digest = hashlib.sha256(encoded).digest()
            cursor = connection.execute("INSERT INTO registros VALUES (?,?,?,?) ON CONFLICT(hash) DO NOTHING",
                                        (digest, payload, source_id, table))
            if cursor.rowcount:
                new_count += 1
            elif connection.execute("SELECT dados FROM registros WHERE hash=?", (digest,)).fetchone()[0] != payload:
                raise ConsolidationError("Colisão de hash detectada; importação e limpeza interrompidas.")
            rows_read += 1
            pending += 1
            if pending == batch_size:
                commit_batch()
                pending = 0
                connection.execute("BEGIN IMMEDIATE")
        if _signature(path) != signature:
            raise ConsolidationError("A origem mudou durante a importação; fontes preservadas.")
        connection.execute("UPDATE fontes SET completo=1 WHERE id=?", (source_id,))
        commit_batch()
    except BaseException:
        connection.rollback()
        raise


def _manifest(connection):
    files = {row[0]: (json.loads(row[1]), row[2]) for row in connection.execute("SELECT caminho, assinatura, sha256 FROM fontes")}
    folders = {row[0]: json.loads(row[1]) for row in connection.execute("SELECT caminho, identidade FROM pastas")}
    return files, folders


def _check_sources(connection, root, excluded, *, cleaning, progress):
    current_files, current_dirs = _inventory(root, excluded)
    files, folders = _manifest(connection)
    if set(current_files) - files.keys() or set(current_dirs) - folders.keys():
        raise ConsolidationError("Há arquivos ou pastas novos na raiz; limpeza cancelada.")
    if not cleaning and (files.keys() != current_files.keys() or folders.keys() != current_dirs.keys()):
        raise ConsolidationError("Uma origem desapareceu antes da limpeza; fontes restantes preservadas.")
    for name, identity in current_dirs.items():
        if identity != folders[name]:
            raise ConsolidationError("Uma pasta foi substituída; limpeza cancelada.")
    for name in current_files:
        _event(progress, "checking_source", arquivo=name)
        if _fingerprint(root / name) != files[name]:
            raise ConsolidationError(f"Origem modificada: {name!r}; limpeza cancelada.")
    return current_files, current_dirs


def _verify(connection, progress):
    _event(progress, "verifying_database")
    if connection.execute("SELECT count(*) FROM fontes WHERE completo=0").fetchone()[0]:
        raise ConsolidationError("Há fontes incompletas; limpeza bloqueada.")
    for (result,) in connection.execute("PRAGMA integrity_check"):
        if result != "ok":
            raise ConsolidationError("A integridade do SQLite falhou; fontes preservadas.")
    count = 0
    for digest, payload in connection.execute("SELECT hash, dados FROM registros"):
        if hashlib.sha256(payload.encode("utf-8")).digest() != digest:
            raise ConsolidationError("Hash de registro inválido; fontes preservadas.")
        count += 1
    expected = connection.execute("SELECT coalesce(sum(novos),0) FROM fontes").fetchone()[0]
    if count != expected:
        raise ConsolidationError("Contagem de registros inconsistente; fontes preservadas.")
    _event(progress, "database_verified", registros=count)
    return count


def _guard(root, relative):
    path = root / relative
    if path == root or not path.is_relative_to(root) or ".." in Path(relative).parts or Path(relative).is_absolute():
        raise ConsolidationError("Caminho fora da raiz recusado.")
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if _link(parent) or os.path.ismount(parent):
            raise ConsolidationError("Link ou montagem apareceu durante a limpeza.")
    return path


def _cleanup(connection, root, excluded, progress):
    state = connection.execute("SELECT valor FROM configuracao WHERE chave='estado'").fetchone()[0]
    files, folders = _check_sources(connection, root, excluded, cleaning=state in {"limpando", "concluido"}, progress=progress)
    connection.execute("UPDATE configuracao SET valor='limpando' WHERE chave='estado'")
    # The verified database and this durable state exist before the first unlink.
    for name, signature in files.items():
        path = _guard(root, name)
        _regular(path)
        if _signature(path) != signature:
            raise ConsolidationError("Arquivo mudou antes da exclusão; limpeza interrompida.")
        path.unlink()
        _event(progress, "source_removed", arquivo=name)
    for name in sorted(folders, key=lambda item: len(Path(item).parts), reverse=True):
        path = _guard(root, name)
        if _signature(path)[:2] != folders[name]:
            raise ConsolidationError("Pasta mudou antes da exclusão; limpeza interrompida.")
        path.rmdir()  # Refuses to remove any unexpected/new contents.
    connection.execute("UPDATE configuracao SET valor='concluido' WHERE chave='estado'")


def _sync_output(path):
    # Windows FlushFileBuffers requires a writable descriptor.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _sync_directory(root):
    if os.name == "posix":
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def consolidate(folder, *, remove_sources=False, encoding="utf-8-sig", delimiter=None, batch_size=10000, progress=None):
    original = Path(folder).absolute()
    root = original.resolve(strict=True)
    if root != original or root == Path(root.anchor) or not root.is_dir() or root.name.casefold() == "backup":
        raise ConsolidationError("Escolha uma pasta de dados real, fora de links, backup e raiz do sistema.")
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 100000:
        raise ConsolidationError("O lote precisa ter entre 1 e 100000 registros.")
    "".encode(encoding)
    if delimiter is not None and (len(delimiter) != 1 or delimiter in '\r\n\0"'):
        raise ConsolidationError("Separador inválido.")
    options = {"encoding": encoding, "delimiter": delimiter, "txt": "lines", "hash": "sha256-canonical-json-v1"}
    final = root / (root.name + ".sqlite")
    pending = root / ("." + root.name + ".importando.sqlite")
    excluded = {final.name, pending.name, final.name + "-journal", pending.name + "-journal"}
    # No outputs are opened before the input tree has passed the inventory.
    lock = root / LOCK
    try:
        lock.mkdir()
    except FileExistsError:
        raise ConsolidationError(f"Reserva {LOCK} já existe. Confirme que não há importador ativo antes de removê-la.") from None
    connection = None
    try:
        _check_outputs(final, pending)
        current_files, current_dirs = _inventory(root, excluded)
        output = final if final.exists() else pending
        create = not output.exists()
        if create and not current_files:
            raise ConsolidationError("Nenhuma fonte CSV, TXT ou SQLite encontrada fora de backup.")
        connection = _connect(output, root, options, create=create)
        state = connection.execute("SELECT valor FROM configuracao WHERE chave='estado'").fetchone()[0]
        if output == pending:
            saved_files, saved_dirs = _manifest(connection)
            if saved_files.keys() - current_files.keys():
                raise ConsolidationError("Uma fonte registrada desapareceu; fontes restantes preservadas.")
            for name, identity in current_dirs.items():
                if name in saved_dirs and saved_dirs[name] != identity:
                    raise ConsolidationError("Uma pasta registrada foi substituída.")
                connection.execute("INSERT OR IGNORE INTO pastas VALUES (?,?)", (name, _json(identity)))
            _event(progress, "discovered_files", count=len(current_files))
            for name in current_files:
                _event(progress, "importing_source", arquivo=name)
                try:
                    _import_source(connection, root, name, options, batch_size, progress)
                except ConsolidationError as error:
                    raise ConsolidationError(f"Fonte {name!r}: {error}") from None
                except (UnicodeError, csv.Error, sqlite3.Error, CatalogError) as error:
                    raise ConsolidationError(f"Falha ao ler {name!r} ({type(error).__name__}); confira codificação, CSV e integridade SQLite. Fontes preservadas.") from None
            count = _verify(connection, progress)
            _check_sources(connection, root, excluded, cleaning=False, progress=progress)
            connection.execute("UPDATE configuracao SET valor='verificado' WHERE chave='estado'")
            connection.close()
            connection = None
            _sync_output(pending)
            if final.exists() or final.is_symlink():
                raise ConsolidationError("O destino apareceu durante a importação; nada foi sobrescrito.")
            pending.rename(final)
            # Persist the rename on Linux before any source is removed.
            _sync_directory(root)
            connection = _connect(final, root, options, create=False)
        else:
            _check_sources(connection, root, excluded, cleaning=state in {"limpando", "concluido"}, progress=progress)
            count = _verify(connection, progress)
        if remove_sources:
            _check_outputs(final, pending)
            _sync_output(final)
            _sync_directory(root)
            _cleanup(connection, root, excluded, progress)
        _sync_output(final)
        summary = {"stage": "complete", "database": str(final), "registros": count,
                   "linhas_origem": connection.execute("SELECT coalesce(sum(linhas),0) FROM fontes").fetchone()[0],
                   "fontes_removidas": remove_sources}
        _event(progress, "complete", **{key: value for key, value in summary.items() if key != "stage"})
        return summary
    finally:
        if connection is not None:
            connection.close()
        lock.rmdir()
