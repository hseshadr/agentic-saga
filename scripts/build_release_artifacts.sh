#!/usr/bin/env bash
set -euo pipefail

refuse() {
  printf 'refusing unsafe output directory: %s\n' "$1" >&2
  exit 2
}

output_arg="${1-}"
[[ -n "$output_arg" ]] || refuse ""
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
dist_root="$repo_root/dist"
working_dir="$(pwd -P)"
if [[ "$output_arg" = /* ]]; then
  requested="$output_arg"
else
  requested="$working_dir/$output_arg"
fi
canonical_dist_root="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$dist_root")"
[[ "$canonical_dist_root" == "$dist_root" ]] || refuse "$output_arg"
canonical_output="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$requested")"
case "$canonical_output" in
  "$dist_root"/*) ;;
  *) refuse "$output_arg" ;;
esac
[[ "$canonical_output" != "$dist_root" ]] || refuse "$output_arg"
[[ ! -L "$requested" ]] || refuse "$output_arg"
[[ ! -e "$requested" || -d "$requested" ]] || refuse "$output_arg"

mkdir -p -- "$dist_root"
output_dir="$canonical_output"
rm -rf -- "$output_dir"
mkdir -p -- "$output_dir"
temp_root="$(mktemp -d)"
trap 'rm -rf "$temp_root"' EXIT
source_root="$temp_root/source"
mkdir -p -- "$source_root"
source_commit="$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}')"
git -C "$repo_root" archive --format=tar "$source_commit" | tar -xf - -C "$source_root"
cd -- "$source_root"
uv export --locked --no-dev --no-emit-project --format requirements.txt \
  -o "$output_dir/runtime-requirements.txt"
uv build --offline --no-python-downloads --no-build-isolation --out-dir "$output_dir"
wheel_count="$(find "$output_dir" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')"
sdist_count="$(find "$output_dir" -maxdepth 1 -name '*.tar.gz' | wc -l | tr -d ' ')"
test "$wheel_count" = "1"
test "$sdist_count" = "1"
wheel="$(find "$output_dir" -maxdepth 1 -name '*.whl' -print -quit)"
sdist="$(find "$output_dir" -maxdepth 1 -name '*.tar.gz' -print -quit)"
sha256() {
  python3 -c \
    'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$1"
}
printf '%s\n' "$source_commit" > "$output_dir/SOURCE_COMMIT"
{
  printf '%s  %s\n' "$(sha256 "$wheel")" "$(basename "$wheel")"
  printf '%s  %s\n' "$(sha256 "$sdist")" "$(basename "$sdist")"
  printf '%s  %s\n' "$(sha256 "$output_dir/runtime-requirements.txt")" "runtime-requirements.txt"
} > "$output_dir/SHA256SUMS"
