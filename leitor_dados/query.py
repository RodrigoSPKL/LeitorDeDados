"""Read-only routed lookups over published SQLite snapshots."""
from __future__ import annotations

import base64
from collections import OrderedDict, defaultdict
from contextlib import closing
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time

from .normalization import normalize, lookup_shard, data_shard, normalization_settings


class DatasetUnavailable(RuntimeError):
    pass


class CursorExpired(ValueError):
    pass


class QueryTimeout(RuntimeError):
    pass


class QueryBusy(RuntimeError):
    pass


class ResultTooLarge(ValueError):
    pass


VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
FIELDS = frozenset({"cpf", "email", "phone", "code"})


def version_path(root: Path, version: str) -> Path:
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("Versão inválida.")
    directory = root / "versions" / version
    if (root / "versions").is_symlink() or directory.is_symlink() or directory.resolve().parent != (root / "versions").resolve():
        raise DatasetUnavailable("Versão indisponível.")
    return directory


def _json_file(path: Path, max_bytes: int = 1_048_576) -> dict:
    if path.is_symlink():
        raise DatasetUnavailable("Metadados inválidos.")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise DatasetUnavailable("Metadados inválidos.")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise DatasetUnavailable("Metadados inválidos.")
    return result


def load_manifest(root: Path, version: str) -> tuple[Path, dict]:
    """Validate names and sizes. Full checksums are an offline operation."""
    directory = version_path(root, version)
    try:
        manifest = _json_file(directory / "manifest.json")
        if manifest.get("schema_version") != 1 or manifest.get("version") != version:
            raise ValueError("Manifesto incompatível.")
        counts = [manifest.get("data_shards"), manifest.get("lookup_shards")]
        if any(type(n) is not int or n < 1 for n in counts) or sum(counts) > 128:
            raise ValueError("Quantidade de arquivos inválida.")
        if type(manifest.get("row_count")) is not int or manifest["row_count"] < 0:
            raise ValueError("Contagem inválida.")
        if not isinstance(manifest.get("normalization"), dict):
            raise ValueError("Normalização inválida.")
        normalization_settings(manifest["normalization"])
        expected = {f"records-{i:04d}.sqlite" for i in range(counts[0])}
        expected |= {f"lookup-{i:04d}.sqlite" for i in range(counts[1])}
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) != len(expected):
            raise ValueError("Lista de arquivos inválida.")
        found = set()
        for entry in entries:
            name = entry["name"]
            if name not in expected or name in found:
                raise ValueError("Arquivo inválido.")
            found.add(name)
            if type(entry.get("bytes")) is not int or entry["bytes"] < 0:
                raise ValueError("Tamanho inválido.")
            if not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")):
                raise ValueError("Checksum inválido.")
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["bytes"]:
                raise ValueError("Arquivo incompleto.")
        return directory, manifest
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise DatasetUnavailable("Acervo indisponível ou incompleto.") from exc


def open_snapshot(path: Path, deadline: float | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA cache_size = -2048")
    if deadline is not None:
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return connection


class DatasetStore:
    def __init__(self, root: Path, cursor_secret: str, max_limit: int = 100,
                 query_timeout_seconds: float = 5.0, max_concurrent_queries: int = 8,
                 cursor_ttl_seconds: int = 86400, max_payload_bytes: int = 8 * 1024 * 1024):
        if not isinstance(cursor_secret, str) or len(cursor_secret) < 32:
            raise ValueError("O segredo de cursor precisa de pelo menos 32 caracteres.")
        if (type(max_limit) is not int or not 1 <= max_limit <= 1000
                or isinstance(query_timeout_seconds, bool)
                or not isinstance(query_timeout_seconds, (float, int))
                or not math.isfinite(query_timeout_seconds) or query_timeout_seconds <= 0
                or type(max_concurrent_queries) is not int or max_concurrent_queries < 1
                or type(cursor_ttl_seconds) is not int or cursor_ttl_seconds <= 0
                or type(max_payload_bytes) is not int or max_payload_bytes < 1):
            raise ValueError("Limites inválidos.")
        self.root = Path(root).resolve()
        self.secret = cursor_secret.encode("utf-8")
        self.max_limit = max_limit
        self.timeout = query_timeout_seconds
        self.cursor_ttl = cursor_ttl_seconds
        self.max_payload_bytes = max_payload_bytes
        self.slots = threading.BoundedSemaphore(max_concurrent_queries)
        self._lock = threading.Lock()
        self._manifests: OrderedDict[str, tuple[Path, dict]] = OrderedDict()

    def _current(self) -> str:
        try:
            version = _json_file(self.root / "current.json")["version"]
            version_path(self.root, version)
            return version
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise DatasetUnavailable("Nenhum acervo publicado.") from exc

    def _snapshot(self, version: str) -> tuple[Path, dict]:
        with self._lock:
            cached = self._manifests.get(version)
            if cached is not None:
                if not (cached[0] / "manifest.json").is_file():
                    del self._manifests[version]
                    raise DatasetUnavailable("Versão indisponível.")
                self._manifests.move_to_end(version)
                return cached
            result = load_manifest(self.root, version)
            self._manifests[version] = result
            if len(self._manifests) > 8:
                self._manifests.popitem(last=False)
            return result

    def ready(self) -> bool:
        try:
            # Readiness also detects removed/incomplete files after an earlier cached search.
            load_manifest(self.root, self._current())
            return True
        except (DatasetUnavailable, ValueError):
            return False

    def info(self) -> dict:
        _, manifest = self._snapshot(self._current())
        return {key: manifest.get(key) for key in
                ("version", "row_count", "data_shards", "lookup_shards", "created_at")}

    def _fingerprint(self, field: str, normalized: str) -> str:
        return hmac.new(self.secret, (field + "\0" + normalized).encode(), hashlib.sha256).hexdigest()

    def _encode(self, version: str, field: str, fingerprint: str, last: int) -> str:
        raw = json.dumps({"v": 1, "dataset": version, "field": field, "fp": fingerprint,
                          "last": last, "iat": int(time.time())}, separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(raw).rstrip(b"=")
        signature = hmac.new(self.secret, body, hashlib.sha256).digest()
        return (body + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode("ascii")

    def _decode(self, cursor: str) -> dict:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError()
            body, signature = cursor.encode("ascii").split(b".")
            actual = base64.b64decode(signature + b"=" * (-len(signature) % 4), altchars=b"-_", validate=True)
            if not hmac.compare_digest(hmac.new(self.secret, body, hashlib.sha256).digest(), actual):
                raise ValueError()
            data = json.loads(base64.b64decode(body + b"=" * (-len(body) % 4), altchars=b"-_", validate=True))
            if not isinstance(data, dict) or data.get("v") != 1:
                raise ValueError()
            if type(data.get("last")) is not int or not 0 < data["last"] < 2**63:
                raise ValueError()
            if type(data.get("iat")) is not int or data["iat"] > time.time() + 60:
                raise ValueError()
            if time.time() - data["iat"] > self.cursor_ttl:
                raise CursorExpired("Cursor expirado.")
            version_path(self.root, data["dataset"])
            return data
        except CursorExpired:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError("Cursor inválido.") from exc

    def search(self, field: str, value: str, limit: int = 50, cursor: str | None = None) -> dict:
        if not isinstance(field, str) or field not in FIELDS or not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("Busca inválida.")
        if type(limit) is not int or not 1 <= limit <= self.max_limit:
            raise ValueError("Limite inválido.")
        if not self.slots.acquire(blocking=False):
            raise QueryBusy("Consultas ocupadas.")
        try:
            return self._search(field, value, limit, cursor)
        finally:
            self.slots.release()

    def _search(self, field: str, value: str, limit: int, cursor: str | None) -> dict:
        deadline = time.monotonic() + self.timeout
        decoded = self._decode(cursor) if cursor else None
        version = decoded["dataset"] if decoded else self._current()
        try:
            directory, manifest = self._snapshot(version)
        except DatasetUnavailable as exc:
            if decoded:
                raise CursorExpired("Versão do cursor indisponível.") from exc
            raise
        normalized = normalize(field, value, manifest["normalization"])
        if not normalized:
            raise ValueError("Identificador vazio.")
        fingerprint = self._fingerprint(field, normalized)
        if decoded and (decoded.get("field") != field or decoded.get("fp") != fingerprint):
            raise ValueError("Cursor não corresponde à busca.")
        after = decoded["last"] if decoded else 0
        try:
            shard = lookup_shard(field, normalized, manifest["lookup_shards"])
            with closing(open_snapshot(directory / f"lookup-{shard:04d}.sqlite", deadline)) as conn:
                ids = [row[0] for row in conn.execute(
                    "SELECT record_id FROM lookup WHERE kind=? AND value=? AND record_id>? "
                    "ORDER BY record_id LIMIT ?", (field, normalized, after, limit + 1))]
            more = len(ids) > limit
            ids = ids[:limit]
            grouped: dict[int, list[int]] = defaultdict(list)
            for record_id in ids:
                grouped[data_shard(record_id, manifest["data_shards"])].append(record_id)
            found = {}
            payload_bytes = 0
            for shard, shard_ids in grouped.items():
                if time.monotonic() >= deadline:
                    raise QueryTimeout("Prazo de consulta excedido.")
                with closing(open_snapshot(directory / f"records-{shard:04d}.sqlite", deadline)) as conn:
                    placeholders = ",".join("?" for _ in shard_ids)
                    for record_id, payload in conn.execute(
                        f"SELECT record_id,payload FROM records WHERE record_id IN ({placeholders})", shard_ids):
                        payload_bytes += len(payload.encode("utf-8"))
                        if payload_bytes > self.max_payload_bytes:
                            raise ResultTooLarge("Reduza a quantidade de registros por página.")
                        decoded_payload = json.loads(payload)
                        if not isinstance(decoded_payload, dict):
                            raise DatasetUnavailable("Conteúdo inválido.")
                        found[record_id] = decoded_payload
            if len(found) != len(ids):
                raise DatasetUnavailable("Conteúdo incompleto.")
            if time.monotonic() >= deadline:
                raise QueryTimeout("Prazo de consulta excedido.")
            return {"version": version, "field": field,
                    "items": [{"record_id": record_id, "data": found[record_id]} for record_id in ids],
                    "next_cursor": self._encode(version, field, fingerprint, ids[-1]) if more else None}
        except sqlite3.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise QueryTimeout("Prazo de consulta excedido.") from exc
            raise DatasetUnavailable("Falha ao consultar o acervo.") from exc
        except (OSError, sqlite3.DatabaseError, json.JSONDecodeError) as exc:
            raise DatasetUnavailable("Falha ao consultar o acervo.") from exc
