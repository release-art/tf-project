#!/bin/bash
# bin/rust/set-version.sh VERSION   write VERSION into rust/Cargo.toml's [package].
# maturin takes the wheel version from there, so a release sets it from the tag before building.
set -euo pipefail
version="${1:?usage: set-version.sh VERSION}"
manifest="$(dirname "$0")/../../rust/Cargo.toml"
# The first `version =` line is [package]'s; dependencies give theirs inline.
awk -v v="$version" '!done && /^version = "/ { print "version = \"" v "\""; done = 1; next } { print }' "$manifest" > "$manifest.tmp"
mv "$manifest.tmp" "$manifest"
grep -m1 '^version = ' "$manifest"
