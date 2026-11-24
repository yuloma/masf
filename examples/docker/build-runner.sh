#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
context=$(mktemp -d)
trap 'rm -rf -- "$context"' EXIT
mkdir "$context/bin" "$context/codex"
for provider in "$@"; do
  case "$provider" in
    codex)
      binary=$(realpath "$(command -v codex)")
      install -m 755 "$binary" "$context/codex/codex"
      for helper in codex-exec-server codex-code-mode-host; do
        source="$(dirname "$binary")/$helper"
        if [[ -x $source ]]; then install -m 755 "$source" "$context/codex/$helper"; fi
      done
      ;;
    cursor)
      release=$(dirname "$(realpath "$(command -v agent)")")
      [[ -f $release/index.js && -x $release/node && -x $release/cursor-agent ]]
      cp -a "$release" "$context/cursor"
      ;;
    claude)
      install -m 755 "$(realpath "$(command -v claude)")" "$context/bin/claude"
      ;;
    *) echo "Proveedor desconocido: $provider" >&2; exit 2 ;;
  esac
done
docker build --target runtime -t masf-runtime:pilot "$root"
docker build -f "$root/examples/docker/Runner.Dockerfile" -t masf-runner:pilot "$context"
