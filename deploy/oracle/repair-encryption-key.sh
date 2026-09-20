#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"

[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE does not exist." >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl is required." >&2; exit 1; }

echo "This replaces FIELD_ENCRYPTION_KEY and creates a timestamped .env backup."
echo "Only continue on a new deployment that has no saved broker credentials."
echo "Changing this key makes existing encrypted broker credentials unreadable."

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Type ROTATE to continue: " confirmation
  [[ "$confirmation" == "ROTATE" ]] || { echo "Cancelled."; exit 1; }
fi

new_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\r\n')"
[[ "$new_key" =~ ^[A-Za-z0-9_-]{43}=$ ]] || {
  echo "ERROR: Failed to generate a valid Fernet key." >&2
  exit 1
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="${ENV_FILE}.backup.${timestamp}"
temporary_file="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
trap 'rm -f "$temporary_file"' EXIT

cp -p "$ENV_FILE" "$backup_file"
awk -v replacement="FIELD_ENCRYPTION_KEY=$new_key" '
  BEGIN { replaced = 0 }
  /^FIELD_ENCRYPTION_KEY=/ {
    if (!replaced) { print replacement; replaced = 1 }
    next
  }
  { print }
  END { if (!replaced) print replacement }
' "$ENV_FILE" > "$temporary_file"

chmod 600 "$temporary_file"
mv "$temporary_file" "$ENV_FILE"
trap - EXIT

echo "FIELD_ENCRYPTION_KEY repaired."
echo "Backup: $backup_file"
echo "Restart with: ./deploy/oracle/reload-all.sh"
