"""Compare the former row writer with sorted batches on generated fictitious data."""
from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from leitor_dados.consolidate import _BatchWriter, _json


def _run(path, engine, rows, payload_bytes, batch_size, cache_mib):
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA cache_size=-{cache_mib * 1024}")
        if engine == "rowid":
            connection.execute("CREATE TABLE registros(id INTEGER PRIMARY KEY,hash BLOB NOT NULL UNIQUE,dados TEXT NOT NULL,fonte_id INTEGER,tabela_origem TEXT)")
        else:
            connection.execute("CREATE TABLE registros(hash BLOB PRIMARY KEY,dados TEXT NOT NULL,fonte_id INTEGER,tabela_origem TEXT) WITHOUT ROWID")
        writer = _BatchWriter(connection)
        new, pending = 0, 0
        batch = {}
        started = perf_counter()
        connection.execute("BEGIN IMMEDIATE")
        for number in range(rows):
            # A quarter of rows deliberately repeat whole records.
            value = number % max(1, rows * 3 // 4)
            payload = _json({"codigo": f"FICTICIO-{value:012}", "texto": "x" * payload_bytes})
            digest = hashlib.sha256(payload.encode("utf-8")).digest()
            if engine == "legacy":
                cursor = connection.execute("INSERT INTO registros VALUES (?,?,?,?) ON CONFLICT(hash) DO NOTHING", (digest, payload, 1, None))
                if cursor.rowcount:
                    new += 1
                elif connection.execute("SELECT dados FROM registros WHERE hash=?", (digest,)).fetchone()[0] != payload:
                    raise ValueError("Colisão no benchmark")
            else:
                batch.setdefault(digest, (payload, None))
            pending += 1
            if pending == batch_size:
                if batch:
                    new += writer.write(batch, 1)
                    batch.clear()
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                pending = 0
        if batch:
            new += writer.write(batch, 1)
        connection.commit()
        elapsed = perf_counter() - started
        fingerprint = hashlib.sha256()
        for digest, payload in connection.execute("SELECT hash,dados FROM registros ORDER BY hash"):
            fingerprint.update(digest)
            fingerprint.update(payload.encode("utf-8"))
        count = connection.execute("SELECT count(*) FROM registros").fetchone()[0]
        if count != new:
            raise ValueError("Contagens do benchmark não coincidem")
    return {"engine": engine, "seconds": round(elapsed, 4), "rows_per_second": round(rows / elapsed, 1),
            "unique": count, "db_bytes": path.stat().st_size, "fingerprint": fingerprint.hexdigest()}


def benchmark(directory, *, rows=150000, payload_bytes=512, batch_size=10000, cache_mib=64, repeats=2, compare_layouts=False):
    if not 1 <= rows <= 5000000 or not 1 <= payload_bytes <= 4096 or not 1 <= repeats <= 10:
        raise ValueError("Use rows 1..5000000, payload-bytes 1..4096 e repeats 1..10")
    if not 1 <= batch_size <= 100000 or not 16 <= cache_mib <= 16384:
        raise ValueError("Configuração de lote/cache inválida")
    base = Path(directory).resolve(strict=True)
    runs = []
    with tempfile.TemporaryDirectory(prefix="bench-consolidacao-", dir=base) as name:
        owned = Path(name).resolve()
        if not owned.is_relative_to(base) or owned == base:
            raise ValueError("Diretório temporário fora do destino")
        for repeat in range(repeats):
            engines = ("legacy", "batch", "rowid") if compare_layouts else ("legacy", "batch")
            if repeat % 2:
                engines = tuple(reversed(engines))
            for engine in engines:
                result = _run(owned / f"{engine}-{repeat}.sqlite", engine, rows, payload_bytes, batch_size, cache_mib)
                runs.append(result)
        if len({run["fingerprint"] for run in runs}) != 1:
            raise ValueError("Os resultados dos dois motores diferem")
    medians = {engine: statistics.median(run["seconds"] for run in runs if run["engine"] == engine)
               for engine in (("legacy", "batch", "rowid") if compare_layouts else ("legacy", "batch"))}
    return {"system": platform.system(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "rows": rows, "payload_bytes": payload_bytes, "batch_size": batch_size, "cache_mib": cache_mib,
            "journal_mode": "DELETE", "synchronous": "FULL", "runs": runs,
            "median_seconds": medians, "speedup": round(medians["legacy"] / medians["batch"], 3),
            "rowid_speedup": round(medians["legacy"] / medians["rowid"], 3) if compare_layouts else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".")
    parser.add_argument("--rows", type=int, default=150000)
    parser.add_argument("--payload-bytes", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--cache-mib", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--compare-layouts", action="store_true")
    args = parser.parse_args()
    print(json.dumps(benchmark(args.directory, rows=args.rows, payload_bytes=args.payload_bytes,
                               batch_size=args.batch_size, cache_mib=args.cache_mib, repeats=args.repeats,
                               compare_layouts=args.compare_layouts), indent=2))
