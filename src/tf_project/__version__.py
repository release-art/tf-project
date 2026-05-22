"""Runtime package-version lookup.

The version is baked into wheel metadata at build time by `pdm-backend`'s
SCM source (driven by git tags like `v1.2.3`). At runtime we read it back
out of the installed distribution's metadata so there is no second source
of truth to keep in sync.

In an editable install with no built dist (e.g. fresh `pdm install` on a
non-tagged commit), `metadata.version` raises `PackageNotFoundError`; we
fall back to a sentinel that's clearly not a real version.
"""

from __future__ import annotations

from importlib import metadata

try:
    __version__ = metadata.version("tf-project")
except metadata.PackageNotFoundError:  # pragma: no cover — editable w/o dist
    __version__ = "0.0.0+unknown"
