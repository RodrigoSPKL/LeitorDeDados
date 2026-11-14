"""Discover archives, append folder/schema tables, and publish a read-only snapshot."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Callable
import uuid

from .archives import ExtractionLimits, discover_files
from .builder import build_dataset, publish_version, _write_json, _fsync_directory
from .catalog import import_files
from .query import DatasetUnavailable, load_manifest


class FolderImportError(ValueError):
    pass


@contextmanager
def _exclusive_work(workspace: Path):
    lock = workspace / ".pipeline-lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise FolderImportError("Esta área já tem uma importação em execução ou uma reserva pendente.") from None
    try:
        yield
    finally:
        lock.rmdir()


def _report(path: Path, report: dict) -> None:
    temporary = path.parent / (".report-" + uuid.uuid4().hex + ".json")
    try:
        _write_json(temporary, report)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def ingest_folder(folder: Path, workspace: Path | None = None, data_dir: Path | None = None,
                  config: dict | None = None, version: str | None = None, publish: bool = True,
                  limits: ExtractionLimits | None = None, excluded_paths: list[Path] | None = None,
                  force_rebuild: bool = False, progress: Callable[[str, int], None] | None = None) -> dict:
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise FolderImportError("A pasta de entrada não existe.")
    original_workspace = Path(workspace) if workspace is not None else folder / ".leitor"
    original_data = Path(data_dir) if data_dir is not None else original_workspace / "published"
    if original_workspace.is_symlink() or original_data.is_symlink():
        raise FolderImportError("Áreas de trabalho e destino não podem ser links simbólicos.")
    workspace, data_dir = original_workspace.resolve(), original_data.resolve()
    if folder == workspace or folder == data_dir or workspace == data_dir:
        raise FolderImportError("Origem, área de trabalho e destino precisam ser pastas diferentes.")
    if workspace in folder.parents or data_dir in folder.parents:
        raise FolderImportError("A origem não pode ficar dentro da área de trabalho ou do destino.")
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - {"catalog", "snapshot"}:
        raise FolderImportError("Configuração de pasta inválida.")
    catalog_options = config.get("catalog", {})
    snapshot_options = config.get("snapshot", {})
    allowed_snapshot = {"data_shards", "lookup_shards", "batch_size", "normalization", "invalid_identifiers"}
    if not isinstance(catalog_options, dict) or not isinstance(snapshot_options, dict) or set(snapshot_options) - allowed_snapshot:
        raise FolderImportError("Configuração de catálogo ou snapshot inválida.")
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o755)
    with _exclusive_work(workspace):
        report = {"status": "discovering", "published": False}
        try:
            excluded = list(excluded_paths or []) + [workspace, data_dir]
            files = discover_files(folder, workspace, limits=limits, excluded_paths=excluded)
            if not files:
                raise FolderImportError("Nenhum CSV, TXT delimitado, JSONL ou banco SQLite foi encontrado.")
            if progress:
                progress("discovered_files", len(files))
            report = import_files(files, workspace / "catalog.sqlite", options=catalog_options,
                                  progress=(lambda rows: progress("catalog_rows_committed", rows)) if progress else None)
            report.update(status="catalog_ready", discovered_files=len(files), published=False)
            tables = report["tables"]
            if not tables:
                raise FolderImportError("As origens não contêm tabelas importáveis.")
            # The catalog contains only TEXT and NULL; it is closed before immutable publication.
            snapshot_config = {"format": "sqlite", "sqlite_table": tables[0]["name"], "fields": {},
                               "invalid_identifiers": "skip", **snapshot_options}
            fingerprint = hashlib.sha256(json.dumps(
                {"tables": tables, "snapshot": snapshot_config}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            if not force_rebuild:
                try:
                    current = json.loads((data_dir / "current.json").read_text(encoding="utf-8"))["version"]
                    _, active = load_manifest(data_dir, current)
                    if ((version is None or version == current) and active.get("dataset_metadata", {})
                            .get("folder_ingestion", {}).get("fingerprint") == fingerprint):
                        report.update(status="unchanged", version=current, published=publish,
                                      snapshot_rebuilt=False, invalid_identifiers=active.get("invalid_identifiers", {}))
                        _report(workspace / "last-report.json", report)
                        return report
                except (OSError, ValueError, KeyError, TypeError, DatasetUnavailable):
                    pass
            version = version or datetime.now(timezone.utc).strftime("folder-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
            inputs = [workspace / "catalog.sqlite"] * len(tables)
            sources = [{"format": "sqlite", "sqlite_table": table["name"], "fields": table["fields"],
                        "metadata": {"table": table["name"], "folder": table["logical_parent"],
                                     "source_table": table.get("source_table")}}
                       for table in tables]
            manifest = build_dataset(inputs, snapshot_config, data_dir, version,
                                     source_configs=sources,
                                     progress=(lambda rows: progress("snapshot_rows_committed", rows)) if progress else None,
                                     dataset_metadata={"folder_ingestion": {"fingerprint": fingerprint,
                                                                            "table_count": len(tables)}})
            if publish:
                publish_version(data_dir, version)
            report.update(status="ok", version=version, published=publish, snapshot_rebuilt=True,
                          invalid_identifiers=manifest.get("invalid_identifiers", {}))
            _report(workspace / "last-report.json", report)
            return report
        except Exception as exc:
            report.update(status="failed", error_type=type(exc).__name__, published=False)
            _report(workspace / "last-report.json", report)
            raise
