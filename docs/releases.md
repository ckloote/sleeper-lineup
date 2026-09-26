# Releases

## Version policy

`0.1.0` is the completed initial release for supervised live use. Reserve `1.0.0`
for proven live operation, including the live shadow gate and review of the
season's validation evidence.

After a release, bump the version with the first subsequent code or documentation
change. Use a patch bump for fixes and documentation, a minor bump for new
capabilities or incompatible changes during 0.x development. Routine raw-data
snapshot captures alone do not change the software version.

Keep `pyproject.toml`, `lockin/__init__.py` and the project's entry in `uv.lock`
in sync. The runtime version is saved with digest model provenance. Do not move
or replace a published release tag; corrections get a new version and tag.

## Release checklist

1. Update the version and lockfile, and add dated notes to `CHANGELOG.md` with
   scope, limitations and validation results.
2. Run the full test suite, Ruff lint and format checks, and `git diff --check`.
   Cron and HTTP tests need local socket access. Tests use a private copy of the
   recorded season database; retain that fixture for historical coverage.
3. Build the wheel and source distribution with `uv build`. Check their version
   metadata and confirm the wheel contains the application and SQL schema.
4. Commit only the intended release files. Exclude local configuration, databases,
   logs and unrelated snapshot captures.
5. Create an annotated `vX.Y.Z` tag on the tested release commit, with release
   notes in its message. Push the release commit and tag together when publishing.
6. Deploy that tag using [deployment.md](deployment.md), preserving backups and
   completing the relevant [season checks](day-one.md).

Tagging a release does not deploy it, change the configured season, or qualify
the live shadow gate.
