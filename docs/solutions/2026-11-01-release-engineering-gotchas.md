# Release engineering: four things that are not obvious

**1. A version bump must edit uv.lock too.** uv records the project's own version in `uv.lock` (`[[package]] name =
"settleview" version = "..."`, the editable source). Changing `pyproject.toml`'s `version` alone makes CI's
`uv lock --check` fail. `scripts/release.py` edits both lines (only those), so no `uv lock` run (and no re-resolution)
is needed for a release.

**2. Trivy's registry credentials and its database.** The release workflow scans a private GHCR image, so Trivy gets
`TRIVY_USERNAME` / `TRIVY_PASSWORD` (the job's read-only `GITHUB_TOKEN`). Those are registry credentials and can be
sent to the vulnerability database's registries too. The workflow downloads the database first, without them, into the
cache volume `scripts/quality.py` uses; the scans then find it fresh. `quality.py --platform` adds `--image-src
remote`, so the scan reads the pushed digest from the registry and never a same-named image in the local Docker.

**3. Parsing YAML without a YAML library.** The project has no PyYAML, and actionlint is not a project tool.
`tests/test_workflows.py` nests each workflow under an `x-` key of an empty compose file and reads it back with
`docker compose config --format json` (compose keeps `x-` keys). Escape every `$` as `$$` first, or compose tries to
interpolate `${{ ... }}`. Compose prints `$$` again in its output: undo it after reading (`replace("$$", "$")`).
`on:` stays the string key `on` (compose's parser is YAML 1.2, not 1.1's boolean).

**4. Rollback and forward-only migrations.** The app checks the schema revision at start (`require_head`) and refuses
a revision its code does not know (exit 75), and the older `migrate` job's `alembic upgrade head` fails with "Can't
locate revision". So an older image never runs on a newer schema: it fails to start instead. Check before a rollback
with Alembic itself: `docker run --rm --network none --entrypoint alembic <image> show <database revision>` (the image's
WORKDIR has alembic.ini; `show` needs no database) exits 0 when that release knows the revision. Most `downgrade()`s
drop columns or tables, so a downgrade is never automatic (docs/operations.md, "Rolling back").
