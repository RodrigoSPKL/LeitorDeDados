#!/usr/bin/env python3
"""Estimativa de armazenamento; não substitui medições em dados reais."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from typing import Sequence


TB = 10**12
TIB = 2**40


@dataclass(frozen=True)
class Assumptions:
    rows: int = 20_000_000_000
    avg_row_bytes: float = 256
    compression_ratio: float = 3
    copies: int = 2
    overhead_pct: float = 20
    free_space_pct: float = 30
    backup_copies: int = 1
    rows_per_day: float | None = None
    target_load_hours: float | None = None


def estimate_capacity(assumptions: Assumptions) -> dict:
    """Calculate a scenario, keeping backups outside the provisioned cluster.

    The average row size is logical, before compression. Overhead estimates
    indexes/metadata and extra materializations as a fraction of compressed
    data. It is applied to every copy. Free space is a fraction of installed
    capacity. Backups assume full independent copies of compressed data plus
    overhead, without cluster replication or the cluster's free-space margin.
    """
    a = assumptions
    for name in ("rows", "copies", "backup_copies"):
        value = getattr(a, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} deve ser um número inteiro.")
    if a.rows <= 0:
        raise ValueError("rows deve ser maior que zero.")
    if a.copies < 1:
        raise ValueError("copies deve ser pelo menos 1 (total de cópias).")
    if a.backup_copies < 0:
        raise ValueError("backup_copies não pode ser negativo.")
    for name in (
        "avg_row_bytes", "compression_ratio", "overhead_pct", "free_space_pct",
        "rows_per_day", "target_load_hours",
    ):
        value = getattr(a, name)
        if value is None and name in ("rows_per_day", "target_load_hours"):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} deve ser numérico.")
        try:
            finite = math.isfinite(value)
        except OverflowError as exc:
            raise ValueError(f"{name} excede a faixa numérica suportada.") from exc
        if not finite:
            raise ValueError(f"{name} deve ser finito (sem NaN ou infinito).")
    if a.avg_row_bytes <= 0:
        raise ValueError("avg_row_bytes deve ser maior que zero.")
    if a.compression_ratio < 1:
        raise ValueError("compression_ratio deve ser pelo menos 1.")
    if a.overhead_pct < 0:
        raise ValueError("overhead_pct não pode ser negativo.")
    if not 0 <= a.free_space_pct < 100:
        raise ValueError("free_space_pct deve estar entre 0 (inclusivo) e 100 (exclusivo).")
    for name in ("rows_per_day", "target_load_hours"):
        value = getattr(a, name)
        if value is not None and value <= 0:
            raise ValueError(f"{name} deve ser maior que zero.")

    try:
        logical = a.rows * a.avg_row_bytes
        compressed = logical / a.compression_ratio
        one_copy = compressed * (1 + a.overhead_pct / 100)
        occupied = one_copy * a.copies
        provisioned = occupied / (1 - a.free_space_pct / 100)
        backups = one_copy * a.backup_copies
        sizes = {
            "logical_data": logical,
            "compressed_data": compressed,
            "one_copy_with_overhead": one_copy,
            "cluster_occupied": occupied,
            "cluster_provisioned": provisioned,
            "backups_separate": backups,
        }
        if not all(math.isfinite(v) for v in sizes.values()):
            raise ValueError("O cenário excede a faixa numérica suportada.")
        result = {
            "assumptions": asdict(a),
            "model": {
                "snapshot_bytes": "rows * avg_row_bytes / compression_ratio * (1 + overhead_pct / 100)",
                "cluster_provisioned_bytes": "snapshot_bytes * copies / (1 - free_space_pct / 100)",
                "backup_bytes": "snapshot_bytes * backup_copies",
                "backup_scope": "Cópias completas separadas; sem réplicas ou folga do cluster.",
                "excluded": ["dados brutos de origem", "WAL/logs", "fila", "versões de registros",
                             "retenção incremental de backups", "RAID", "tráfego"],
                "caveat": "Compressão e overhead são hipóteses configuráveis, não valores universais ou medidos.",
            },
            "storage": {
                name: {"bytes": value, "TB": value / TB, "TiB": value / TIB}
                for name, value in sizes.items()
            },
            "growth_days_from_empty": (
                a.rows / a.rows_per_day if a.rows_per_day is not None else None
            ),
            "initial_load_rows_per_second": (
                a.rows / a.target_load_hours / 3600
                if a.target_load_hours is not None else None
            ),
        }
        for name in ("growth_days_from_empty", "initial_load_rows_per_second"):
            value = result[name]
            if value is not None and not math.isfinite(value):
                raise ValueError("O cenário excede a faixa numérica suportada.")
        return result
    except OverflowError as exc:
        raise ValueError("O cenário excede a faixa numérica suportada.") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Estima armazenamento para um cenário. Compressão e overhead são "
            "hipóteses, não resultados medidos. TB = 10^12 bytes; TiB = 2^40 bytes."
        ),
        epilog=(
            "Cluster = linhas × bytes/linha ÷ compressão × (1 + overhead/100) "
            "× cópias ÷ (1 - espaço livre/100). Backups são calculados à parte: "
            "uma cópia comprimida com overhead × backup-copies, sem multiplicar "
            "pelas réplicas nem pela margem do cluster. Não inclui retenção "
            "incremental, WAL/logs, fila, dados brutos, versões, tráfego, RAID ou recuperação. "
            "O tempo de crescimento parte de uma base vazia, sem expiração."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--rows", type=int, default=20_000_000_000,
                        help="Total de registros a armazenar")
    parser.add_argument("--avg-row-bytes", type=float, default=256,
                        help="Média de bytes lógicos por registro, antes da compressão")
    parser.add_argument("--compression-ratio", type=float, default=3,
                        help="Razão lógico/comprimido; 3 significa redução para 1/3")
    parser.add_argument("--copies", type=int, default=2,
                        help="Total de cópias no cluster, incluindo a primária")
    parser.add_argument("--overhead-pct", type=float, default=20,
                        help="Espaço adicional sobre os dados comprimidos por cópia (%%)")
    parser.add_argument("--free-space-pct", type=float, default=30,
                        help="Parcela da capacidade instalada que deve ficar livre (%%)")
    parser.add_argument("--backup-copies", type=int, default=1,
                        help="Cópias completas de backup, armazenadas separadamente")
    parser.add_argument("--rows-per-day", type=float,
                        help="Crescimento diário; permite estimar dias até o total")
    parser.add_argument("--target-load-hours", type=float,
                        help="Prazo da carga inicial; calcula vazão média necessária")
    parser.add_argument("--json", action="store_true", help="Emite JSON")
    return parser


def format_pt_br(value: int | float, decimal_places: int | None = None) -> str:
    """Format a number without depending on the operating system's locale."""
    if decimal_places is not None:
        formatted = f"{value:,.{decimal_places}f}"
    elif isinstance(value, int):
        formatted = f"{value:,}"
    else:
        formatted = f"{value:,g}"
    return formatted.translate(str.maketrans(",.", ".,"))


def human_report(result: dict) -> str:
    a = result["assumptions"]
    lines = [
        "Estimativa de cenário — hipóteses, não dimensionamento validado",
        f"Registros: {format_pt_br(a['rows'])}",
        f"Bytes lógicos/registro: {format_pt_br(a['avg_row_bytes'])}",
        f"Compressão lógico/comprimido: {format_pt_br(a['compression_ratio'])}:1",
        f"Cópias no cluster (total): {format_pt_br(a['copies'])}",
        f"Overhead por cópia: {format_pt_br(a['overhead_pct'])}%",
        f"Espaço livre da capacidade instalada: {format_pt_br(a['free_space_pct'])}%",
        f"Cópias completas de backup separado: {format_pt_br(a['backup_copies'])}",
        "",
    ]
    labels = {
        "logical_data": "Dados lógicos, sem compressão",
        "compressed_data": "Dados comprimidos, uma cópia",
        "one_copy_with_overhead": "Uma cópia com overhead",
        "cluster_occupied": "Cluster ocupado, todas as cópias",
        "cluster_provisioned": "Cluster a provisionar, incluindo espaço livre",
        "backups_separate": "Backups em armazenamento separado",
    }
    for key, label in labels.items():
        size = result["storage"][key]
        lines.append(
            f"{label}: {format_pt_br(size['TB'], 3)} TB / "
            f"{format_pt_br(size['TiB'], 3)} TiB"
        )
    if result["growth_days_from_empty"] is not None:
        lines.append(
            f"Crescimento: {format_pt_br(a['rows_per_day'])} registros/dia; "
            f"{format_pt_br(result['growth_days_from_empty'], 2)} dias até o total (base vazia, sem expiração)."
        )
    if result["initial_load_rows_per_second"] is not None:
        lines.append(
            f"Carga inicial em {format_pt_br(a['target_load_hours'])} horas: "
            f"{format_pt_br(result['initial_load_rows_per_second'], 2)} registros/s em média."
        )
    lines.extend([
        "",
        "TB = 10^12 bytes; TiB = 2^40 bytes.",
        "Backups não incluem réplicas nem a reserva de espaço livre do cluster.",
        "Não inclui WAL/logs, fila, dados brutos, versões, retenção incremental, RAID ou tráfego.",
        "Meça bytes/registro e compressão com amostra representativa antes de comprar infraestrutura.",
    ])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = vars(parser.parse_args(argv))
    json_output = args.pop("json")
    try:
        result = estimate_capacity(Assumptions(**args))
    except ValueError as exc:
        parser.error(str(exc))
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        print(human_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
