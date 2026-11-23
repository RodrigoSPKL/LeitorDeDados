"""Run from a data folder; the installed project itself must stay elsewhere."""
from pathlib import Path
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from leitor_dados.consolidate import consolidate, ConsolidationError


def main():
    parser = argparse.ArgumentParser(description="CSV, TXT por linha e SQLite para um SQLite com deduplicação SHA-256 global.")
    parser.add_argument("--folder", default=".", help="Pasta de dados; padrão: pasta atual.")
    parser.add_argument("--apagar-fontes", action="store_true", help="Apagar fontes e pastas após importar e verificar tudo. Preserva backup.")
    parser.add_argument("--encoding", default="utf-8-sig", help="Codificação de CSV/TXT; ex.: cp1252. Padrão UTF-8 com BOM opcional.")
    parser.add_argument("--delimiter", help="Separador CSV explícito; padrão: detectar , ; TAB |.")
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args()
    try:
        consolidate(args.folder, remove_sources=args.apagar_fontes, encoding=args.encoding,
                    delimiter=args.delimiter, batch_size=args.batch_size,
                    progress=lambda event: print(json.dumps(event, ensure_ascii=True), flush=True))
    except KeyboardInterrupt:
        print("Interrompido. Fontes ainda não removidas continuam no disco; execute novamente com as mesmas opções.", file=sys.stderr)
        return 130
    except sqlite3.Error:
        print("Erro SQLite. Importação/limpeza interrompida; fontes restantes preservadas. Confira espaço livre e integridade do banco.", file=sys.stderr)
        return 1
    except (ConsolidationError, OSError, ValueError, LookupError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
