#!/usr/bin/env bash
set -euo pipefail

refuse() {
  printf 'refusing unsafe release directory: %s\n' "$1" >&2
  exit 2
}

sha256() {
  python3 -c \
    'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$1"
}

release_arg="${1-}"
[[ -n "$release_arg" ]] || refuse ""
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
dist_root="$repo_root/dist"
working_dir="$(pwd -P)"
if [[ "$release_arg" = /* ]]; then
  requested="$release_arg"
else
  requested="$working_dir/$release_arg"
fi
canonical_dist_root="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$dist_root")"
canonical_release="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$requested")"
case "$canonical_release" in
  "$canonical_dist_root"/*) ;;
  *) refuse "$release_arg" ;;
esac
[[ "$canonical_release" != "$canonical_dist_root" ]] || refuse "$release_arg"
[[ ! -L "$requested" && -d "$canonical_release" ]] || refuse "$release_arg"

release_python="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "$release_python" == "3.12" || "$release_python" == "3.13" ]] || {
  printf 'runtime wheelhouse requires Python 3.12 or 3.13\n' >&2
  exit 1
}
runtime_requirements="$canonical_release/runtime-requirements.txt"
source_commit="$canonical_release/SOURCE_COMMIT"
manifest="$canonical_release/SHA256SUMS"
[[ -s "$runtime_requirements" && -s "$source_commit" && -s "$manifest" ]] || {
  printf 'missing immutable release artifact manifests\n' >&2
  exit 1
}
recorded_commit="$(<"$source_commit")"
[[ "$recorded_commit" =~ ^[0-9a-f]{40}$ ]] || {
  printf 'invalid committed source marker\n' >&2
  exit 1
}

wheel_count="$(find "$canonical_release" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')"
sdist_count="$(find "$canonical_release" -maxdepth 1 -name '*.tar.gz' | wc -l | tr -d ' ')"
[[ "$wheel_count" == "1" && "$sdist_count" == "1" ]] || {
  printf 'release artifacts must contain one wheel and one sdist\n' >&2
  exit 1
}
wheel="$(find "$canonical_release" -maxdepth 1 -name '*.whl' -print -quit)"
sdist="$(find "$canonical_release" -maxdepth 1 -name '*.tar.gz' -print -quit)"
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
[[ "$(sha256 "$wheel")" == "$wheel_digest" && "$(sha256 "$sdist")" == "$sdist_digest" ]] || {
  printf 'artifact digest mismatch\n' >&2
  exit 1
}

wheelhouses="$canonical_release/wheelhouses"
mkdir -p -- "$wheelhouses"
[[ ! -L "$wheelhouses" ]] || refuse "$release_arg"
target="$wheelhouses/$release_python"
[[ ! -e "$target" ]] || {
  printf 'runtime wheelhouse already exists: %s\n' "$target" >&2
  exit 1
}
temporary="$(mktemp -d "$wheelhouses/.${release_python}.tmp.XXXXXX")"
trap 'rm -rf "$temporary"' EXIT
PIP_NO_INPUT=1 python3 -m pip download --disable-pip-version-check \
  --only-binary=:all: --require-hashes --dest "$temporary" \
  --requirement "$runtime_requirements"
[[ -n "$(find "$temporary" -maxdepth 1 -name '*.whl' -print -quit)" ]] || {
  printf 'runtime wheelhouse contains no wheels\n' >&2
  exit 1
}
python3 -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$temporary" "$target"
trap - EXIT
