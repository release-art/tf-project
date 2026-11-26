# Development

tf-project is written in Rust; the code is in `rust/`. Python only packages it:
[maturin](https://www.maturin.rs/) puts the compiled program into a wheel.

## Setup

Install [rustup](https://rustup.rs/). The first `cargo` command in `rust/`
installs the toolchain pinned in `rust/rust-toolchain.toml`. The checks also
need three cargo tools:

```sh
cargo install cargo-nextest cargo-llvm-cov cargo-deny
```

## Everyday work

```sh
bin/rust/check.sh           # everything CI checks (below)
bin/rust/check.sh --fix     # format and apply clippy's fixes first
bin/run.sh status           # run tfp from the source tree
pip install .               # build and install the wheel locally
```

`bin/rust/check.sh` runs, in order:

1. `cargo fmt --check`;
2. clippy with pedantic lints, and no `unwrap`, `expect`, `panic` or indexing
   outside tests;
3. `cargo doc` with warnings as errors (every public item is documented);
4. `cargo deny`: licences, advisories, duplicate and unknown crates;
5. the tests, under coverage, which must stay at 100% of lines, regions and
   functions.

The tests need none of Terraform, `op`, `az` or `aws`: they use small fake
scripts in place of those, and a fake Key Vault. Run them with nextest, as
`check.sh` does, which gives each test its own process.

## Releasing

Releases are driven by git tags; the version is never edited in the code.
`rust/Cargo.toml` says `0.0.0`, and the release workflow writes the tag into
it (`bin/rust/set-version.sh`) before building.

1. In `CHANGELOG.md`, rename the `## [Unreleased]` section to
   `## [X.Y.Z] - YYYY-MM-DD` and commit that to `main`.
2. Tag the commit and push the tag:

   ```sh
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

The **Release** workflow then:

- re-runs CI on the tagged commit;
- builds a wheel for each platform (Linux x86_64 and arm64, macOS arm64 and x86_64,
  Windows x86_64) and the source distribution;
- packs each platform's programs as a standalone `.tar.gz` / `.zip`;
- signs everything with [Sigstore](https://www.sigstore.dev/);
- publishes the wheels and source distribution to PyPI;
- creates a GitHub release with the changelog section and all the files;
- for a stable release, points the Homebrew formula in
  [release-art/homebrew-tap](https://github.com/release-art/homebrew-tap) at
  the new archives (`bin/homebrew-formula.py`), committed by the Release.art
  workflow bot.

A tag like `v1.2.0rc1` (also `a1`, `b1`) makes a pre-release on GitHub and
PyPI. There is no TestPyPI step: the name `tf-project` isn't available there.

One-time setup the workflow relies on:

- **PyPI:** trusted publishing, set up on PyPI for this repository's
  `release.yml` and its `release` GitHub environment.
- **Homebrew:** the *Release.art workflow bot* GitHub App, installed on
  `release-art/homebrew-tap` with *Contents: read and write*. The workflow
  signs in with the organisation variable `RELEASE_ART_WORKFLOW_APP_ID` and
  secret `RELEASE_ART_WORKFLOW_APP_PK`.
