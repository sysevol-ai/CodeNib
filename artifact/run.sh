#!/usr/bin/env bash
set -euo pipefail

artifact_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mode="${1:-full}"
default_output="$(dirname "$artifact_root")/codeminer-artifact-output"
output_dir="${2:-$default_output}"

run_native() {
  local venv="${CODEMINER_ARTIFACT_VENV:-${TMPDIR:-/tmp}/codeminer-artifact-venv}"
  local mpl_cache="${CODEMINER_ARTIFACT_MPL_CACHE:-${TMPDIR:-/tmp}/codeminer-artifact-matplotlib}"
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install --disable-pip-version-check \
    -r "$artifact_root/figure/requirements-paper.txt"
  mkdir -p "$mpl_cache"
  MPLBACKEND=Agg \
  MPLCONFIGDIR="$mpl_cache" \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED=0 \
    "$venv/bin/python" "$artifact_root/artifact_eval.py" full \
      --artifact-root "$artifact_root" \
      --output-dir "$output_dir"
}

run_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "docker is required for container mode" >&2
    exit 2
  }
  local output_parent output_name
  mkdir -p "$(dirname "$output_dir")"
  output_parent="$(cd "$(dirname "$output_dir")" && pwd)"
  output_name="$(basename "$output_dir")"
  if [[ -e "$output_dir" ]]; then
    echo "output directory already exists: $output_dir" >&2
    exit 2
  fi
  docker build -t codeminer-artifact:local "$artifact_root"
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$output_parent:/results" \
    codeminer-artifact:local full \
      --output-dir "/results/$output_name"
}

case "$mode" in
  smoke)
    PYTHONDONTWRITEBYTECODE=1 \
      python3 "$artifact_root/artifact_eval.py" smoke \
        --artifact-root "$artifact_root"
    ;;
  full)
    run_native
    ;;
  docker)
    run_docker
    ;;
  *)
    echo "usage: $0 [smoke|full|docker] [output-directory]" >&2
    exit 2
    ;;
esac
