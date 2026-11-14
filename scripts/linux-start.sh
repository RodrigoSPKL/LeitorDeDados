#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

command -v python3 >/dev/null || { echo 'Instale Python 3.10+ no host.' >&2; exit 1; }
command -v docker >/dev/null || { echo 'Instale Docker Engine e Docker Compose.' >&2; exit 1; }
docker compose version >/dev/null

# Container IDs must match host IDs for these file-based credentials.
SECURITY_OPTIONS="$(docker info --format '{{json .SecurityOptions}}')"
if [[ "$SECURITY_OPTIONS" == *name=rootless* || "$SECURITY_OPTIONS" == *name=userns* ]]; then
  echo 'Este início exige Docker rootful sem userns-remap. Consulte docs/rodar-linux.md para as permissões dos volumes.' >&2
  exit 1
fi

DATA_PATH="${DATA_PATH:-$PROJECT_DIR/data}"
mkdir -p "$DATA_PATH" inputs secrets .leitor
DATA_PATH="$(CDPATH= cd -- "$DATA_PATH" && pwd -P)"
export DATA_PATH
chmod 0755 "$DATA_PATH"
# File-based credentials retain host ownership. Prepare only these two files
# on a host filesystem that supports per-file POSIX ownership (not exFAT).
SECRET_SETUP=(python3 "$PROJECT_DIR/scripts/init_secrets.py" --directory "$PROJECT_DIR/secrets" --uid 10001 --gid 10001)
if (( EUID == 0 )); then
  "${SECRET_SETUP[@]}"
else
  command -v sudo >/dev/null || { echo 'Execute este script como root para preparar as credenciais locais.' >&2; exit 1; }
  sudo "${SECRET_SETUP[@]}"
fi
docker compose build

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
