#!/bin/bash
# bin/rust/set-version.sh VERSION   write VERSION (PEP 440, as tagged: 1.2.3, 1.2.3rc1) into rust/Cargo.toml.
# maturin takes the wheel version from there, so a release sets it from the tag before building. Cargo wants semver
# for pre-releases (1.2.3-rc.1); maturin turns that back into the PEP 440 form for the wheel.
set -euo pipefail
version="${1:?usage: set-version.sh VERSION}"
version=$(echo "$version" | sed -E 's/^([0-9.]+)a([0-9]+)$/\1-alpha.\2/; s/^([0-9.]+)b([0-9]+)$/\1-beta.\2/; s/^([0-9.]+)rc([0-9]+)$/\1-rc.\2/')
manifest="$(dirname "$0")/../../rust/Cargo.toml"
# The first `version =` line is [package]'s; dependencies give theirs inline.
awk -v v="$version" '!done && /^version = "/ { print "version = \"" v "\""; done = 1; next } { print }' "$manifest" > "$manifest.tmp"
mv "$manifest.tmp" "$manifest"
grep -m1 '^version = ' "$manifest"
