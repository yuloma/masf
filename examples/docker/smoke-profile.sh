#!/usr/bin/env bash
set -euo pipefail

profile_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../profiles" && pwd -P)
engine_socket=${MASF_DOCKER_SOCKET:-/var/run/docker.sock}
engine=(docker --host "unix://$engine_socket")
"${engine[@]}" compose version >/dev/null
"${engine[@]}" info >/dev/null

scratch=$(mktemp -d "${TMPDIR:-/tmp}/masf-smoke-XXXXXXXXXX")
scratch=$(cd -- "$scratch" && pwd -P)
prefix=$(basename -- "$scratch" | tr '[:upper:]' '[:lower:]')
started=()

stack() {
  local stage=$1
  shift
  MASF_WORKSPACE="$scratch/$stage" "${engine[@]}" compose \
    --env-file /dev/null --project-directory "$profile_dir" \
    -p "$prefix-$stage" -f "$profile_dir/postgres.yaml" "$@"
}

cleanup() {
  local status=$? stage
  trap - EXIT
  for stage in "${started[@]}"; do
    stack "$stage" down --volumes --remove-orphans || status=1
  done
  rm -rf -- "$scratch"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

umask 022
for stage in base candidate; do
  mkdir -p "$scratch/$stage/tests"
  echo 'CREATE TABLE probe (id integer PRIMARY KEY);' > "$scratch/$stage/schema.sql"
  cat > "$scratch/$stage/tests/smoke.sql" <<'SQL'
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM probe) THEN
    RAISE EXCEPTION 'Expected an empty database for this run';
  END IF;
END; $$;
SQL
  started+=("$stage")
  stack "$stage" up -d --wait --wait-timeout 90
  stack "$stage" exec -T app psql -X -v ON_ERROR_STOP=1 -f schema.sql
  stack "$stage" exec -T app psql -X -v ON_ERROR_STOP=1 -f tests/smoke.sql
  if [[ $stage == base ]]; then
    stack base exec -T app psql -X -v ON_ERROR_STOP=1 -c 'INSERT INTO probe VALUES (1)'
  fi
done

count=$(stack base exec -T app psql -XAt -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM probe')
[[ $count == 1 ]] || { echo 'La base perdió el dato de control.' >&2; exit 1; }
echo 'El candidato empezó vacío y la base conserva su dato de control.'
