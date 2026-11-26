#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$root"

fail() { printf '%s\n' "$*" >&2; exit 1; }

configure_control() {
  export MASF_ROOT=${MASF_ROOT:-$HOME/.local/share/masf-pilot}
  local wrapper=${MASF_CONTROL:-$MASF_ROOT/control}
  if [[ -n ${MASF_CONTROL:-} || -e $wrapper ]]; then
    [[ -x $wrapper ]] || fail "El control existente no es ejecutable: $wrapper"
    control=("$wrapper")
    return
  fi
  export MASF_CONFIG_DIR=${MASF_CONFIG_DIR:-$MASF_ROOT/config}
  export MASF_STATE_DIR=${MASF_STATE_DIR:-$MASF_ROOT/state}
  export MASF_JOBS_DIR=${MASF_JOBS_DIR:-$MASF_ROOT/jobs}
  export MASF_UID=${MASF_UID:-$(id -u)} MASF_GID=${MASF_GID:-$(id -g)}
  export MASF_DOCKER_SOCKET=${MASF_DOCKER_SOCKET:-/var/run/docker.sock}
  export MASF_DOCKER_GID=${MASF_DOCKER_GID:-$(stat -c %g "$MASF_DOCKER_SOCKET")}
  export MASF_CONTROLLER_IMAGE=${MASF_CONTROLLER_IMAGE:-masf-controller:pilot}
  [[ -f $MASF_CONFIG_DIR/pilot.toml ]] || fail "Falta el registro: $MASF_CONFIG_DIR/pilot.toml. Sigue INSTALL.md."
  [[ -d $MASF_STATE_DIR && -d $MASF_JOBS_DIR ]] || fail "Faltan los directorios de estado o trabajos. Sigue INSTALL.md."
  # Keep the credentials out of command arguments and Compose output.
  if [[ -z ${GH_TOKEN:-} && -z ${GITHUB_TOKEN:-} ]] && command -v gh >/dev/null; then
    GH_TOKEN=$(gh auth token 2>/dev/null) || true
  fi
  export GH_TOKEN=${GH_TOKEN:-${GITHUB_TOKEN:-}}
  control=(docker --host "unix://$MASF_DOCKER_SOCKET" compose --env-file /dev/null
    -p "${MASF_PROJECT:-masf-control}" -f "$root/examples/docker/compose.yaml"
    -f "$root/examples/docker/engine.yaml")
}

build_controller() {
  docker --host "unix://${MASF_DOCKER_SOCKET:-/var/run/docker.sock}" build \
    --target controller --build-arg "MASF_REVISION=$(git rev-parse HEAD)" \
    -t "${MASF_CONTROLLER_IMAGE:-masf-controller:pilot}" "$root"
}

build_runner() {
  local providers
  read -r -a providers <<< "${MASF_PROVIDERS:-codex cursor}"
  bash "$root/examples/docker/build-runner.sh" "${providers[@]}"
}

sync_main() {
  local branch=${MASF_MAIN_BRANCH:-main} remote=${MASF_REMOTE:-origin} revision
  [[ $(git symbolic-ref --quiet --short HEAD) == "$branch" ]] || \
    fail "La actualización requiere un checkout de $branch; conserva tu rama de desarrollo."
  [[ -z $(git status --porcelain) ]] || fail "El checkout tiene cambios locales; guárdalos antes de actualizar."
  git fetch -- "$remote" "$branch"
  revision=$(git rev-parse FETCH_HEAD)
  git merge-base --is-ancestor HEAD "$revision" || \
    fail "La rama local contiene commits ajenos a $remote/$branch; no se modifica la instalación."
  git merge --ff-only "$revision"
}

case ${1:-help} in
  help)
    cat <<'HELP'
Uso: make <receta> [VARIABLE=valor]

  start           Arrancar el controlador de la instalación existente
  stop            Detener el controlador conservando estado y trabajos
  restart         Recrear el controlador con la imagen local
  ps              Consultar el contenedor del controlador
  status          Consultar trabajos y PR de la fábrica
  logs            Mostrar las últimas 100 líneas del controlador
  logs-follow     Seguir los logs del controlador
  doctor          Diagnosticar registro, GitHub, Docker y proveedores
  doctor-offline  Validar el registro sin consultas externas
  scan            Mostrar issues elegibles sin registrarlas (dry-run)
  build           Construir controlador y runner (MASF_PROVIDERS='codex cursor')
  build-controller Construir solo el controlador
  build-runner    Construir solo el runner con sus CLIs locales
  update          Traer origin/main, construir, validar y recrear la instalación
  test            Ejecutar la suite local con proveedores y GitHub simulados
  test-engine     Ejecutar checks y recuperación con Docker real
  smoke           Comprobar el aislamiento de PostgreSQL con Docker real
  check           Validar el diff y la sintaxis del script

Se reutiliza MASF_CONTROL o, si existe, ~/.local/share/masf-pilot/control.
Sin wrapper se usan los manifiestos del checkout y las variables MASF_*.
Ejecuta update y restart con la cola en reposo. Consulta docs/operacion.md.
HELP
    ;;
  build) build_controller; build_runner ;;
  build-controller) build_controller ;;
  build-runner) build_runner ;;
  update)
    sync_main
    # Reload the script from the fetched revision before installing it.
    exec bash "$root/scripts/factory.sh" install-update
    ;;
  start|stop|restart|ps|status|logs|logs-follow|doctor|doctor-offline|scan|install-update)
    configure_control
    case $1 in
      start) "${control[@]}" up -d controller ;;
      stop) "${control[@]}" stop controller ;;
      restart) "${control[@]}" up -d --force-recreate controller ;;
      ps) "${control[@]}" ps controller ;;
      status) "${control[@]}" exec -T controller masf --config /etc/masf/pilot.toml status ;;
      logs) "${control[@]}" logs --tail 100 controller ;;
      logs-follow) "${control[@]}" logs --tail 100 --follow controller ;;
      doctor) "${control[@]}" run --rm --no-deps controller doctor ;;
      doctor-offline) "${control[@]}" run --rm --no-deps controller doctor --offline ;;
      scan) "${control[@]}" run --rm --no-deps controller scan --dry-run ;;
      install-update)
        build_controller
        build_runner
        "${control[@]}" run --rm --no-deps controller doctor --offline
        "${control[@]}" up -d --force-recreate controller
        "${control[@]}" ps controller
        "${control[@]}" exec -T controller masf --config /etc/masf/pilot.toml status
        ;;
    esac
    ;;
  *) fail "Receta desconocida: $1. Consulta make help." ;;
esac
