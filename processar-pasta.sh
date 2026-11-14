#!/usr/bin/env bash
set -euo pipefail

# The input is the caller's working directory, even when this script lives elsewhere.
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INPUT_DIR="$(pwd -P)"
command -v docker >/dev/null || { echo 'Instale Docker Engine para executar este comando.' >&2; exit 1; }
mkdir -p "$INPUT_DIR/.leitor/published" "$INPUT_DIR/.leitor/tmp"
docker build -t leitor-dados:local "$PROJECT_DIR"
docker run --rm --user "$(id -u):$(id -g)" \
  -e TMPDIR=/work/tmp \
  --mount "type=bind,src=$INPUT_DIR,dst=/scan,readonly" \
  --mount "type=bind,src=$INPUT_DIR/.leitor,dst=/work" \
  --mount "type=bind,src=$INPUT_DIR/.leitor/published,dst=/data" \
  --mount "type=bind,src=$PROJECT_DIR/config,dst=/config,readonly" \
  --entrypoint python leitor-dados:local -m leitor_dados ingest-folder \
  --folder /scan --work-dir /work --data-dir /data --exclude /scan/.leitor "$@"
