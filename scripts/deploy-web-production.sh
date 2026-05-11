#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${CHABO_APP_DIR:-/opt/chabo}"
ENV_FILE="${CHABO_ENV_FILE:-/etc/chabo/env}"
PUBLIC_HOST="${CHABO_DEPLOY_HOST:-${1:-}}"
HEALTH_URL="${CHABO_DEPLOY_HEALTH_URL:-${2:-}}"
SERVICE_NAME="${CHABO_API_SERVICE:-chabo-api}"

if [[ -z "$PUBLIC_HOST" && -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
  PUBLIC_HOST="${CHABO_DEPLOY_HOST:-${CHABO_PUBLIC_HOST:-}}"
fi

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "usage: $0 <public-host> [health-url]" >&2
  echo "or set CHABO_PUBLIC_HOST in $ENV_FILE" >&2
  exit 2
fi

if [[ -z "$HEALTH_URL" ]]; then
  HEALTH_URL="https://${PUBLIC_HOST}/api/health"
fi

cd "$APP_DIR"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
fi

if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="${CHABO_PYTHON:-$APP_DIR/.venv/bin/python}"
else
  PYTHON_BIN="${CHABO_PYTHON:-python3}"
fi

if [[ -x "$APP_DIR/.venv/bin/chabo" ]]; then
  CHABO_BIN="${CHABO_BIN:-$APP_DIR/.venv/bin/chabo}"
else
  CHABO_BIN="${CHABO_BIN:-chabo}"
fi

if [[ "${CHABO_DEPLOY_PULL:-1}" == "1" ]]; then
  git pull --ff-only
fi

if [[ "${CHABO_DEPLOY_BACKUP:-1}" == "1" ]]; then
  BACKUP_TARGET="${CHABO_DEPLOY_BACKUP_TARGET:-}"
  if [[ -n "$BACKUP_TARGET" ]]; then
    "$CHABO_BIN" backup-db --target "$BACKUP_TARGET"
  else
    "$CHABO_BIN" backup-db
  fi
fi

"$PYTHON_BIN" -m pip install -e ".[web]"

if [[ "${CHABO_DEPLOY_NPM_CI:-1}" == "1" ]]; then
  (cd web && npm ci)
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  SYSTEMCTL=(systemctl)
else
  SYSTEMCTL=(sudo systemctl)
fi

"$CHABO_BIN" verify-web --profile production \
  --host "$PUBLIC_HOST" \
  --health-url "$HEALTH_URL" \
  --audit-chain \
  --skip-health

"${SYSTEMCTL[@]}" restart "$SERVICE_NAME"
sleep "${CHABO_DEPLOY_HEALTH_DELAY:-2}"

"$CHABO_BIN" verify-web --profile production \
  --host "$PUBLIC_HOST" \
  --health-url "$HEALTH_URL" \
  --audit-chain \
  --skip-tests \
  --skip-build

echo "ChaBo web production deploy verified: $PUBLIC_HOST"
echo "Rollback hint: stop $SERVICE_NAME, restore the backup SQLite, then restart and rerun verify-web."
