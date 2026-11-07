#!/bin/sh
set -eu

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${TEST_DATABASE_NAME:?TEST_DATABASE_NAME is required}"

export DATABASE_URL="postgresql+psycopg://${POSTGRES_USER:-webagent}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}/${TEST_DATABASE_NAME}"

python -m alembic upgrade head
exec python -m alembic current
