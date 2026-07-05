#!/usr/bin/env bash
set -euo pipefail

: "${PGHOST:?PGHOST must be set}"
: "${PGPORT:?PGPORT must be set}"
: "${PGDATABASE:?PGDATABASE must be set}"
: "${PGUSER:?PGUSER must be set}"
: "${PGPASSWORD:?PGPASSWORD must be set}"
: "${XIAN_BDS_USER:?XIAN_BDS_USER must be set}"
: "${XIAN_POSTGRAPHILE_USER:?XIAN_POSTGRAPHILE_USER must be set}"
: "${XIAN_POSTGRAPHILE_PASSWORD:?XIAN_POSTGRAPHILE_PASSWORD must be set}"
: "${XIAN_POSTGRAPHILE_STATEMENT_TIMEOUT_MS:=10000}"

if ! [[ "${XIAN_POSTGRAPHILE_STATEMENT_TIMEOUT_MS}" =~ ^[0-9]+$ ]]; then
  printf 'XIAN_POSTGRAPHILE_STATEMENT_TIMEOUT_MS must be a non-negative integer\n' >&2
  exit 1
fi

psql \
  --no-psqlrc \
  --set ON_ERROR_STOP=1 \
  --set "owner_role=${XIAN_BDS_USER}" \
  --set "postgraphile_user=${XIAN_POSTGRAPHILE_USER}" \
  --set "postgraphile_password=${XIAN_POSTGRAPHILE_PASSWORD}" \
  --set "postgraphile_statement_timeout_ms=${XIAN_POSTGRAPHILE_STATEMENT_TIMEOUT_MS}" \
  <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'postgraphile_user',
  :'postgraphile_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'postgraphile_user'
)\gexec

SELECT format(
  'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'postgraphile_user',
  :'postgraphile_password'
)\gexec

SELECT format(
  'ALTER ROLE %I SET statement_timeout = %L',
  :'postgraphile_user',
  :'postgraphile_statement_timeout_ms' || 'ms'
)\gexec

SELECT format(
  'ALTER ROLE %I SET idle_in_transaction_session_timeout = %L',
  :'postgraphile_user',
  :'postgraphile_statement_timeout_ms' || 'ms'
)\gexec

SELECT format(
  'GRANT CONNECT ON DATABASE %I TO %I',
  current_database(),
  :'postgraphile_user'
)\gexec

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'postgraphile_user')\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :'postgraphile_user')\gexec
SELECT format(
  'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I',
  :'postgraphile_user'
)\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO %I',
  :'owner_role',
  :'postgraphile_user'
)\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I',
  :'owner_role',
  :'postgraphile_user'
)\gexec
SQL
