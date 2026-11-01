# Trivy's msgpack / urllib3 / setuptools findings were pip's own vendored copies

Found while adding the Trivy gate (2026-10-31).

**Problem.** Trivy reported HIGH CVEs in `msgpack` 1.1.2, `urllib3` 2.7.0 and `setuptools` 70.3.0 in the image. The
app does not depend on msgpack at all, and `pip list` in the image showed urllib3 2.8.0 and setuptools 84.x: bumping
pins in pyproject.toml changed nothing.

**Cause.** The findings had no `PkgPath` and sat in the base image's layer: they are the copies pip vendors in
`pip/_vendor/` (`vendor.txt`: msgpack 1.1.2, urllib3 2.7.0, setuptools 70.3.0 for `pkg_resources`). No pip release
up to 26.2.2 vendors fixed versions, so upgrading pip does not help either.

**Fix.** The Dockerfile uninstalls pip after the install (`pip uninstall --yes pip` in the same RUN): nothing uses it
at run time, the root filesystem is read-only anyway, and the whiteout hides the base layer's files from scanners.
The app's own urllib3 and setuptools are pinned in pyproject.toml.

**How to avoid.** For a Trivy finding, look at the JSON's `PkgPath` and `Layer` before touching dependencies: no path
plus a base-image layer usually means a vendored copy (pip, or a bundled wheel), which the project's pins cannot reach.
