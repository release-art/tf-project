"""bin/scoop-manifest.py VERSION   print the Scoop manifest for tf-project VERSION.

Reads the release's Windows archive (tf-project-<version>-x86_64-pc-windows-msvc.zip) and its SHA-256 from GitHub
with `gh release view`, so the manifest always matches what was published. The release workflow writes the output
to release-art/scoop-bucket's bucket/tf-project.json.
"""

import json
import subprocess
import sys

REPO = "release-art/tf-project"
TARGET = "x86_64-pc-windows-msvc"

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
archive = f"tf-project-{version}-{TARGET}.zip"
if archive not in digests:
    sys.exit(f"v{version} has no {archive}")

download = f"https://github.com/{REPO}/releases/download"
manifest = {
    "version": version,
    "description": "Terraform/OpenTofu wrapper with one slot per tfvars environment",
    "homepage": f"https://github.com/{REPO}",
    "license": "MIT",
    "architecture": {
        "64bit": {
            "url": f"{download}/v{version}/{archive}",
            "hash": digests[archive],
            "extract_dir": f"tf-project-{version}-{TARGET}",
        }
    },
    "bin": ["tfp.exe", "tf-project.exe"],
    # For Scoop's own tools (`scoop checkver`, autoupdate); the release workflow keeps the manifest current anyway.
    "checkver": {"github": f"https://github.com/{REPO}"},
    "autoupdate": {
        "architecture": {
            "64bit": {
                "url": f"{download}/v$version/tf-project-$version-{TARGET}.zip",
                "extract_dir": f"tf-project-$version-{TARGET}",
            }
        }
    },
}
print(json.dumps(manifest, indent=4))
