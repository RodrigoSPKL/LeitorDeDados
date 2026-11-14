#!/usr/bin/env bash
set -euo pipefail

# The input is the caller's working directory, even when this script lives elsewhere.
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INPUT_DIR="$(pwd -P)"
command -v docker >/dev/null || { echo 'Instale Docker Engine para executar este comando.' >&2; exit 1; }
WORK_PATH="$(realpath -m -- "${WORK_PATH:-$INPUT_DIR/.leitor}")"
if [[ "$WORK_PATH" == / || "$WORK_PATH" == "$INPUT_DIR" || "$INPUT_DIR" == "$WORK_PATH/"* ]]; then
  echo 'WORK_PATH deve ser uma pasta separada e não pode conter a pasta de entrada.' >&2
  exit 1
fi
mkdir -p "$WORK_PATH/published" "$WORK_PATH/tmp"
EXCLUDES=(--exclude /scan/.leitor)
if [[ "$WORK_PATH" == "$INPUT_DIR/"* ]]; then
  EXCLUDES+=(--exclude "/scan/${WORK_PATH#"$INPUT_DIR/"}")
fi
docker build -t leitor-dados:local "$PROJECT_DIR"
docker run --rm --user "$(id -u):$(id -g)" \
  -e TMPDIR=/work/tmp \
  --mount "type=bind,src=$INPUT_DIR,dst=/scan,readonly" \
  --mount "type=bind,src=$WORK_PATH,dst=/work" \
  --mount "type=bind,src=$WORK_PATH/published,dst=/data" \
  --mount "type=bind,src=$PROJECT_DIR/config,dst=/config,readonly" \
  --entrypoint python leitor-dados:local -m leitor_dados ingest-folder \
  --folder /scan --work-dir /work --data-dir /data "${EXCLUDES[@]}" "$@"
