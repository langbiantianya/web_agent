#!/bin/sh
set -eu

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${TEST_DATABASE_NAME:?TEST_DATABASE_NAME is required}"

export TEST_DATABASE_URL="postgresql+psycopg://${POSTGRES_USER:-webagent}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}/${TEST_DATABASE_NAME}"

exec python -m pytest tests/test_api_admin_auth.py \
  -q -p no:langsmith --timeout=180 -o cache_dir=/tmp/pytest-cache "$@"
