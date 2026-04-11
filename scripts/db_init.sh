#!/usr/bin/env bash
# =============================================================================
# NestEgg - db_init.sh
# Initializes the database schema and seeds reference data.
# Usage: ./scripts/db_init.sh
# Requires: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_DATABASE
#           set in environment or sourced from ../.env
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="${SCRIPT_DIR}/../database"

# Source .env if present and not already set
if [[ -f "${SCRIPT_DIR}/../.env" ]]; then
  # shellcheck disable=SC1091
  set -o allexport
  source "${SCRIPT_DIR}/../.env"
  set +o allexport
fi

: "${DB_HOST:=localhost}"
: "${DB_PORT:=3306}"
: "${DB_USER:?DB_USER must be set}"
: "${DB_PASSWORD:?DB_PASSWORD must be set}"
: "${DB_DATABASE:?DB_DATABASE must be set}"

DB_CMD=(mysql
  -h "${DB_HOST}"
  -P "${DB_PORT}"
  -u "${DB_USER}"
  "-p${DB_PASSWORD}"
  "${DB_DATABASE}"
)

echo "==> Connecting to MySQL at ${DB_HOST}:${DB_PORT} / ${DB_DATABASE}"

echo "==> Applying schema (01_schema.sql)..."
"${DB_CMD[@]}" < "${DB_DIR}/01_schema.sql"

echo "==> Seeding reference data (02_seed.sql)..."
"${DB_CMD[@]}" < "${DB_DIR}/02_seed.sql"

echo "==> Done. NestEgg database is ready."
