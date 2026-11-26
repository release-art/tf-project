"""bin/homebrew-formula.py VERSION   print the Homebrew formula for tf-project VERSION.

Reads the release's standalone archives (tf-project-<version>-<target>.tar.gz) and their SHA-256 from GitHub with
`gh release view`, so the formula always matches what was published. Platforms without an archive in that release
are left out. The release workflow writes the output to release-art/homebrew-tap's Formula/tf-project.rb.
"""

import json
import subprocess
import sys

REPO = "release-art/tf-project"
# Homebrew's platform blocks, in formula order, and the Rust target each one installs.
PLATFORMS = [
    ("on_macos", "on_arm", "aarch64-apple-darwin"),
    ("on_macos", "on_intel", "x86_64-apple-darwin"),
    ("on_linux", "on_intel", "x86_64-unknown-linux-gnu"),
    ("on_linux", "on_arm", "aarch64-unknown-linux-gnu"),
]

version = sys.argv[1].removeprefix("v")
release = json.loads(
    subprocess.run(
        ["gh", "release", "view", f"v{version}", "--repo", REPO, "--json", "assets"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
)
digests = {a["name"]: a["digest"].removeprefix("sha256:") for a in release["assets"]}

blocks: dict[str, list[str]] = {}
for os_block, arch_block, target in PLATFORMS:
    name = f"tf-project-{version}-{target}.tar.gz"
    if name in digests:
        url = f"https://github.com/{REPO}/releases/download/v{version}/{name}"
        blocks.setdefault(os_block, []).append(
            f'    {arch_block} do\n      url "{url}"\n      sha256 "{digests[name]}"\n    end\n'
        )
if not blocks:
    sys.exit(f"v{version} has no tf-project-{version}-<target>.tar.gz archives")

platforms = "\n".join(f"  {os_block} do\n{''.join(arches)}  end\n" for os_block, arches in blocks.items())
print(
    f'''class TfProject < Formula
  desc "Terraform/OpenTofu wrapper with one slot per tfvars environment"
  homepage "https://github.com/{REPO}"
  license "MIT"

  livecheck do
    url :stable
    strategy :github_latest
  end

{platforms}
  def install
    bin.install "tf-project", "tfp"
  end

  test do
    assert_equal version.to_s, shell_output("#{{bin}}/tfp --version").strip
    assert_match "Could not find tf_project.toml", shell_output("#{{bin}}/tfp status 2>&1", 1)
  end
end''',
)
