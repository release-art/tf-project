#!/bin/bash -e
# Run tfp from the source tree.
exec cargo run --quiet --manifest-path "$(dirname "${BASH_SOURCE[0]}")/../rust/Cargo.toml" --bin tfp -- "$@"
