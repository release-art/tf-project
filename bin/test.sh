#!/bin/bash -e
# Every gate for the Rust crate; see bin/rust/check.sh.
exec "$(dirname "${BASH_SOURCE[0]}")/rust/check.sh" "$@"
