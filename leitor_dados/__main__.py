"""Command line: prepare, validate, publish, and serve static snapshots."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from .builder import build_dataset, publish_version
from .query import DatasetUnavailable, load_manifest, open_snapshot


def verify_dataset(root: Path, version: str, checksums: bool = False, full: bool = False) -> dict:
    directory, manifest = load_manifest(root.resolve(), version)
    for entry in manifest["files"]:
        path = directory / entry["name"]
        if checksums:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != entry["sha256"]:
                raise ValueError("Checksum divergente; a versão não deve ser publicada.")
        with closing(open_snapshot(path)) as connection:
            statement = "PRAGMA integrity_check" if full else "PRAGMA quick_check"
            if connection.execute(statement).fetchall() != [("ok",)]:
                raise ValueError("Integridade inválida.")
            expected_table = "records" if entry["name"].startswith("records-") else "lookup"
            if connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name=?",
                                  (expected_table,)).fetchone() is None:
                raise ValueError("Esquema inválido.")
    return {"version": version, "files_verified": len(manifest["files"]),
            "checksums": checksums, "integrity": "full" if full else "quick", "status": "ok"}


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Preparar e consultar acervos SQLite estáticos.")
    sub = cli.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Importar, indexar e finalizar uma versão nova")
    sources = build.add_mutually_exclusive_group(required=True)
    sources.add_argument("--input", type=Path, nargs="+")
    sources.add_argument("--sources", type=Path, help="Plano JSON para formatos/mapeamentos diferentes")
    build.add_argument("--config", type=Path, required=True)
    build.add_argument("--data-dir", type=Path, default=Path("data"))
    build.add_argument("--version", required=True)
    build.add_argument("--publish", action="store_true")
    publish = sub.add_parser("publish", help="Publicar uma versão já finalizada")
    publish.add_argument("--data-dir", type=Path, default=Path("data"))
    publish.add_argument("--version", required=True)
    verify = sub.add_parser("verify", help="Verificação offline; pode ler todos os arquivos")
    verify.add_argument("--data-dir", type=Path, default=Path("data"))
    verify.add_argument("--version", required=True)
    verify.add_argument("--checksums", action="store_true")
    verify.add_argument("--full", action="store_true", help="Integridade completa, mais cara")
    serve = sub.add_parser("serve", help="Iniciar a API; configure os arquivos de segredos")
    serve.add_argument("--data-dir", type=Path, default=Path("data"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workers", type=int, default=2)
    folder = sub.add_parser("ingest-folder", help="Extrair e importar recursivamente a pasta atual")
    folder.add_argument("--folder", type=Path, default=Path.cwd())
    folder.add_argument("--work-dir", type=Path)
    folder.add_argument("--data-dir", type=Path)
    folder.add_argument("--config", type=Path, help="Configuração opcional de catálogo e snapshot")
    folder.add_argument("--version")
    folder.add_argument("--no-publish", action="store_true")
    folder.add_argument("--force-rebuild", action="store_true")
    folder.add_argument("--exclude", type=Path, action="append", default=[])
    folder.add_argument("--max-depth", type=int, default=12)
    folder.add_argument("--max-members", type=int, default=100000)
    folder.add_argument("--max-expanded-gib", type=int, default=100)
    folder.add_argument("--max-file-gib", type=int, default=20)
    return cli


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.command == "build":
            config = json.loads(args.config.read_text(encoding="utf-8-sig"))
            source_configs = None
            inputs = args.input
            if args.sources is not None:
                plan = json.loads(args.sources.read_text(encoding="utf-8-sig"))
                if not isinstance(plan, list) or not 1 <= len(plan) <= 10000:
                    raise ValueError("Plano de origens inválido.")
                inputs, source_configs = [], []
                for source in plan:
                    if (not isinstance(source, dict) or not isinstance(source.get("path"), str)
                            or not source["path"].strip()):
                        raise ValueError("Origem do plano inválida.")
                    source_path = Path(source["path"])
                    if not source_path.is_absolute():
                        source_path = args.sources.resolve().parent / source_path
                    inputs.append(source_path)
                    source_configs.append({key: val for key, val in source.items() if key != "path"})
            last_report = 0
            def progress(rows: int) -> None:
                nonlocal last_report
                if rows - last_report >= 100_000:
                    print(json.dumps({"stage": "import", "rows_committed": rows}), file=sys.stderr, flush=True)
                    last_report = rows
            result = build_dataset(inputs, config, args.data_dir, args.version,
                                   publish=args.publish, progress=progress, source_configs=source_configs)
            print(json.dumps({"version": result["version"], "row_count": result["row_count"],
                              "published": args.publish}, ensure_ascii=False))
        elif args.command == "publish":
            publish_version(args.data_dir, args.version)
            print(json.dumps({"published": args.version}))
        elif args.command == "verify":
            print(json.dumps(verify_dataset(args.data_dir, args.version, args.checksums, args.full)))
        elif args.command == "ingest-folder":
            from .folder_pipeline import ingest_folder
            from .archives import ExtractionLimits
            config = json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config else None
            limits = ExtractionLimits(max_depth=args.max_depth, max_members=args.max_members,
                                      max_expanded_bytes=args.max_expanded_gib * 1024**3,
                                      max_single_file_bytes=args.max_file_gib * 1024**3)
            def folder_progress(stage: str, count: int) -> None:
                print(json.dumps({"stage": stage, "count": count}), file=sys.stderr, flush=True)
            result = ingest_folder(args.folder, args.work_dir, args.data_dir, config=config,
                                   version=args.version, publish=not args.no_publish, limits=limits,
                                   excluded_paths=args.exclude, force_rebuild=args.force_rebuild,
                                   progress=folder_progress)
            print(json.dumps(result, ensure_ascii=False))
        else:
            if not 1 <= args.port <= 65535 or not 1 <= args.workers <= 32:
                cli.error("Porta ou quantidade de processos inválida.")
            os.environ["DATA_DIR"] = str(args.data_dir.resolve())
            import uvicorn
            uvicorn.run("leitor_dados.api:create_app", factory=True, host=args.host,
                        port=args.port, workers=args.workers, access_log=False)
        return 0
    except (ValueError, OSError, sqlite3.Error, DatasetUnavailable) as exc:
        # Nunca imprimir o traceback ou uma linha de entrada contendo dados pessoais.
        if isinstance(exc, (OSError, sqlite3.Error)):
            message = "Falha de arquivo ou SQLite; verifique caminhos, permissões e espaço disponível."
        elif isinstance(exc, json.JSONDecodeError):
            message = "Arquivo JSON inválido."
        else:
            message = str(exc)
        print(f"Erro: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
