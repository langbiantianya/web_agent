#!/bin/sh
set -eu

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${TEST_DATABASE_NAME:?TEST_DATABASE_NAME is required}"

export ADMIN_DATABASE_URL="postgresql+psycopg://${POSTGRES_USER:-webagent}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}/${TEST_DATABASE_NAME}"
export ADMIN_PUBLIC_BASE_URL="${ADMIN_PUBLIC_BASE_URL:-http://127.0.0.1:18001/admin}"
export ADMIN_SESSION_COOKIE_SECURE=false
export SECRET_KEY="${SECRET_KEY:-admin-smoke-test-secret-key-0123456789}"

exec uvicorn services.admin_web.app:app --host 0.0.0.0 --port 8001
