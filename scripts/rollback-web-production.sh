#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${CHABO_APP_DIR:-/opt/chabo}"
ENV_FILE="${CHABO_ENV_FILE:-/etc/chabo/env}"
DRY_RUN="${CHABO_ROLLBACK_DRY_RUN:-0}"

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

BACKUP_PATH="${1:-${CHABO_ROLLBACK_BACKUP:-}}"
PUBLIC_HOST="${CHABO_DEPLOY_HOST:-${2:-}}"
HEALTH_URL="${CHABO_DEPLOY_HEALTH_URL:-${3:-}}"
SERVICE_NAME="${CHABO_API_SERVICE:-chabo-api}"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
  PUBLIC_HOST="${PUBLIC_HOST:-${CHABO_DEPLOY_HOST:-${CHABO_PUBLIC_HOST:-}}}"
fi

if [[ -z "$BACKUP_PATH" ]]; then
  echo "usage: $0 [--dry-run] <backup-sqlite-path> [public-host] [health-url]" >&2
  echo "or set CHABO_ROLLBACK_BACKUP before running." >&2
  exit 2
fi

if [[ -z "${CHABO_DB_PATH:-}" ]]; then
  echo "CHABO_DB_PATH must be set in the environment or $ENV_FILE" >&2
  exit 2
fi

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "public host is required as arg 2, CHABO_DEPLOY_HOST, or CHABO_PUBLIC_HOST in $ENV_FILE" >&2
  exit 2
fi

if [[ -z "$HEALTH_URL" ]]; then
  HEALTH_URL="https://${PUBLIC_HOST}/api/health"
fi

if [[ -x "$APP_DIR/.venv/bin/chabo" ]]; then
  CHABO_BIN="${CHABO_BIN:-$APP_DIR/.venv/bin/chabo}"
else
  CHABO_BIN="${CHABO_BIN:-chabo}"
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  SYSTEMCTL=(systemctl)
else
  SYSTEMCTL=(sudo systemctl)
fi

run() {
  echo "+ $*"
  if [[ "$DRY_RUN" != "1" ]]; then
    "$@"
  fi
}

if [[ "$DRY_RUN" != "1" && ! -f "$BACKUP_PATH" ]]; then
  echo "backup file not found: $BACKUP_PATH" >&2
  exit 2
fi

cd "$APP_DIR"

CURRENT_BACKUP="${CHABO_ROLLBACK_CURRENT_BACKUP:-$(dirname "$CHABO_DB_PATH")/backups/pre-rollback-$(date +%Y%m%d-%H%M%S).sqlite3}"
run mkdir -p "$(dirname "$CURRENT_BACKUP")"
run "$CHABO_BIN" backup-db --target "$CURRENT_BACKUP"
run "${SYSTEMCTL[@]}" stop "$SERVICE_NAME"
run install -m 0600 "$BACKUP_PATH" "$CHABO_DB_PATH"
run "${SYSTEMCTL[@]}" start "$SERVICE_NAME"
sleep "${CHABO_ROLLBACK_HEALTH_DELAY:-2}"
run "$CHABO_BIN" verify-web --profile production \
  --host "$PUBLIC_HOST" \
  --health-url "$HEALTH_URL" \
  --audit-chain \
  --skip-tests \
  --skip-build

echo "ChaBo web rollback verified: $PUBLIC_HOST"
echo "Current DB safety backup: $CURRENT_BACKUP"
