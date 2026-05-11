#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${CHABO_APP_DIR:-/opt/chabo}"
ENV_FILE="${CHABO_ENV_FILE:-/etc/chabo/env}"
STRICT="${CHABO_AUDIT_REPORT_STRICT:-0}"
CREATED_FROM="${CHABO_AUDIT_REPORT_CREATED_FROM:-}"
CREATED_TO="${CHABO_AUDIT_REPORT_CREATED_TO:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --strict)
      STRICT=1
      shift
      ;;
    --created-from)
      CREATED_FROM="${2:?missing value for --created-from}"
      shift 2
      ;;
    --created-to)
      CREATED_TO="${2:?missing value for --created-to}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
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

REPORT_DIR="${CHABO_AUDIT_REPORT_DIR:-${CHABO_DB_PATH:+$(dirname "$CHABO_DB_PATH")/audit-reports}}"
REPORT_DIR="${REPORT_DIR:-/var/lib/chabo/audit-reports}"
mkdir -p "$REPORT_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
report_path="$REPORT_DIR/audit-chain-$timestamp.json"
checksum_path="$report_path.sha256"
args=(verify-audit-chain)

if [[ "$STRICT" == "1" ]]; then
  args+=(--strict)
fi
if [[ -n "$CREATED_FROM" ]]; then
  args+=(--created-from "$CREATED_FROM")
fi
if [[ -n "$CREATED_TO" ]]; then
  args+=(--created-to "$CREATED_TO")
fi

cd "$APP_DIR"
"${CHABO_CMD[@]}" "${args[@]}" > "$report_path"
shasum -a 256 "$report_path" > "$checksum_path"

echo "Audit integrity report: $report_path"
echo "Checksum: $checksum_path"
