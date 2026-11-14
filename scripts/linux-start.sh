#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

command -v python3 >/dev/null || { echo 'Instale Python 3.10+ no host.' >&2; exit 1; }
command -v docker >/dev/null || { echo 'Instale Docker Engine e Docker Compose.' >&2; exit 1; }
docker compose version >/dev/null

DATA_PATH="${DATA_PATH:-$PROJECT_DIR/data}"
mkdir -p "$DATA_PATH" inputs secrets
DATA_PATH="$(CDPATH= cd -- "$DATA_PATH" && pwd -P)"
export DATA_PATH
chmod 0755 "$DATA_PATH"
python3 scripts/init_secrets.py
docker compose build

# File-based Compose secrets retain host ownership. Set only these two files to
# the API UID, preserving their content. This assumes rootful Docker on Linux.
docker compose --profile tools run --rm --no-deps --entrypoint python tools \
  /app/scripts/init_secrets.py --directory /secrets --uid 10001 --gid 10001

if [[ ! -f "$DATA_PATH/current.json" ]]; then
  echo 'Nenhum acervo publicado: preparando demonstração com dados fictícios.'
  docker compose --profile tools run --rm --no-deps tools build \
    --config /config/import.example.json \
    --input /examples/registros.csv \
    --version demo-v1 --data-dir /data --publish
fi

docker compose up -d --wait --wait-timeout 90 api
docker compose ps api
echo 'API iniciada em http://127.0.0.1:8000. Consulte docs/rodar-linux.md.'
