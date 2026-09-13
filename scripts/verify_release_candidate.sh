#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:?usage: verify_release_candidate.sh OUTPUT_DIR}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
working_dir="$(pwd -P)"
if [[ "$output_dir" != /* ]]; then
  output_dir="$working_dir/$output_dir"
fi
output_dir="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$output_dir")"
dist_root="$repo_root/dist"
canonical_dist_root="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$dist_root")"
case "$output_dir" in
  "$canonical_dist_root"/*) ;;
  *)
    printf 'refusing unsafe output directory: %s\n' "$output_dir" >&2
    exit 2
    ;;
esac
[[ "$output_dir" != "$canonical_dist_root" ]] || {
  printf 'refusing unsafe output directory: %s\n' "$output_dir" >&2
  exit 2
}
intended_commit="$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}')"
if [[ ! -d "$output_dir" ]]; then
  bash "$script_dir/build_release_artifacts.sh" "$output_dir"
fi
wheel="$(find "$output_dir" -maxdepth 1 -name '*.whl' -print -quit)"
sdist="$(find "$output_dir" -maxdepth 1 -name '*.tar.gz' -print -quit)"
wheel_count="$(find "$output_dir" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')"
sdist_count="$(find "$output_dir" -maxdepth 1 -name '*.tar.gz' | wc -l | tr -d ' ')"
[[ "$wheel_count" == "1" && "$sdist_count" == "1" ]] || {
  printf 'release candidate must contain one wheel and one sdist\n' >&2
  exit 1
}
source_commit_file="$output_dir/SOURCE_COMMIT"
manifest="$output_dir/SHA256SUMS"
[[ -s "$source_commit_file" ]] || {
  printf 'missing committed source marker: %s\n' "$source_commit_file" >&2
  exit 1
}
[[ -s "$manifest" ]] || {
  printf 'missing artifact digest manifest: %s\n' "$manifest" >&2
  exit 1
}
recorded_commit="$(<"$source_commit_file")"
[[ "$recorded_commit" == "$intended_commit" ]] || {
  printf 'release candidate commit does not match HEAD\n' >&2
  exit 1
}
current_commit="$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}')"
[[ "$current_commit" == "$intended_commit" ]] || {
  printf 'HEAD changed during release verification\n' >&2
  exit 1
}
sha256() {
  python3 -c \
    'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$1"
}
verify_digest() {
  expected="$1"
  artifact="$2"
  actual="$(sha256 "$artifact")"
  [[ "$actual" == "$expected" ]] || {
    printf 'artifact digest mismatch: %s\n' "$(basename "$artifact")" >&2
    exit 1
  }
}
wheel_name="$(basename "$wheel")"
sdist_name="$(basename "$sdist")"
wheel_digest=""
sdist_digest=""
manifest_lines=0
while read -r expected filename extra; do
  [[ -n "$expected" && -n "$filename" && -z "${extra:-}" ]] || {
    printf 'invalid artifact digest manifest\n' >&2
    exit 1
  }
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || {
    printf 'invalid artifact digest manifest\n' >&2
    exit 1
  }
  case "$filename" in
    "$wheel_name")
      [[ -z "$wheel_digest" ]] || { printf 'duplicate artifact digest\n' >&2; exit 1; }
      wheel_digest="$expected"
      ;;
    "$sdist_name")
      [[ -z "$sdist_digest" ]] || { printf 'duplicate artifact digest\n' >&2; exit 1; }
      sdist_digest="$expected"
      ;;
    *)
      printf 'unknown artifact in digest manifest: %s\n' "$filename" >&2
      exit 1
      ;;
  esac
  manifest_lines=$((manifest_lines + 1))
done < "$manifest"
[[ "$manifest_lines" == "2" && -n "$wheel_digest" && -n "$sdist_digest" ]] || {
  printf 'incomplete artifact digest manifest\n' >&2
  exit 1
}
verify_digest "$wheel_digest" "$wheel"
verify_digest "$sdist_digest" "$sdist"
runtime_requirements="$output_dir/runtime-requirements.txt"
[[ -s "$runtime_requirements" ]] || {
  printf 'missing locked runtime requirements: %s\n' "$runtime_requirements" >&2
  exit 1
}
release_python="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "$release_python" == "3.12" || "$release_python" == "3.13" ]] || {
  printf 'release verification requires Python 3.12 or 3.13\n' >&2
  exit 1
}
wheelhouses="$output_dir/wheelhouses"
[[ ! -L "$wheelhouses" ]] || {
  printf 'refusing unsafe runtime wheelhouse directory: %s\n' "$wheelhouses" >&2
  exit 2
}
wheelhouse="$wheelhouses/$release_python"
if [[ ! -d "$wheelhouse" ]]; then
  bash "$script_dir/build_runtime_wheelhouse.sh" "$output_dir"
fi
[[ ! -L "$wheelhouse" && -d "$wheelhouse" && -n "$(find "$wheelhouse" -maxdepth 1 -name '*.whl' -print -quit)" ]] || {
  printf 'missing offline runtime wheelhouse: %s\n' "$wheelhouse" >&2
  exit 1
}
temp_root="$(mktemp -d)"
trap 'rm -rf "$temp_root"' EXIT

cd -- "$repo_root"
venv="$temp_root/venv"
UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never uv venv --offline --no-python-downloads \
  --python "$release_python" "$venv"
UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never uv pip install --offline --no-python-downloads \
  --require-hashes --no-index --find-links "$wheelhouse" \
  --python "$venv/bin/python" -r "$runtime_requirements"
UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never uv pip install --offline --no-python-downloads --no-deps \
  --python "$venv/bin/python" "$wheel"
UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never uv pip check --offline --no-python-downloads \
  --python "$venv/bin/python"
actual="$("$venv"/bin/agentic-saga --version)"
test "$actual" = "agentic-saga 0.1.0"
"$venv"/bin/python -c \
  'from agentic_saga.agents import DeepAgentsDriver, OpenRouterSettings, build_openrouter_driver'
