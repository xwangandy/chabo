#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${CHABO_APP_DIR:-/opt/chabo}"
ENV_FILE="${CHABO_ENV_FILE:-/etc/chabo/env}"
BACKUP_PATH="${1:-${CHABO_REHEARSE_BACKUP:-}}"
HOST="${CHABO_REHEARSE_HOST:-127.0.0.1}"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
fi

if [[ -z "$BACKUP_PATH" ]]; then
  echo "usage: $0 <backup-sqlite-path>" >&2
  echo "or set CHABO_REHEARSE_BACKUP before running." >&2
  exit 2
fi

if [[ -z "${CHABO_DB_PATH:-}" ]]; then
  echo "CHABO_DB_PATH must be set in the environment or $ENV_FILE" >&2
  exit 2
fi

if [[ ! -f "$BACKUP_PATH" ]]; then
  echo "backup file not found: $BACKUP_PATH" >&2
  exit 2
fi

if [[ -n "${CHABO_BIN:-}" ]]; then
  CHABO_CMD=("$CHABO_BIN")
elif [[ -x "$APP_DIR/.venv/bin/chabo" ]]; then
  CHABO_CMD=("$APP_DIR/.venv/bin/chabo")
elif command -v chabo >/dev/null 2>&1; then
  CHABO_CMD=(chabo)
else
  CHABO_CMD=(python3 -m chabo.cli)
fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/chabo-restore-rehearsal.XXXXXX")"
if [[ "${CHABO_REHEARSE_KEEP:-0}" != "1" ]]; then
  trap 'rm -rf "$work_dir"' EXIT
fi

current_copy="$work_dir/current.sqlite3"
restored_copy="$work_dir/restored.sqlite3"
cp "$CHABO_DB_PATH" "$current_copy"
cp "$BACKUP_PATH" "$restored_copy"

cd "$APP_DIR"
CHABO_DB_PATH="$restored_copy" "${CHABO_CMD[@]}" init-db
CHABO_DB_PATH="$restored_copy" "${CHABO_CMD[@]}" verify-audit-chain
CHABO_DB_PATH="$restored_copy" "${CHABO_CMD[@]}" preflight --host "$HOST" --allow-weak-tokens --allow-dev-auth-bypass

echo "SQLite restore rehearsal passed."
echo "Current DB copy: $current_copy"
echo "Restored backup copy: $restored_copy"
