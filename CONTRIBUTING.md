# Contributing

Contributions are welcome. Keep the runtime standard-library only and preserve
the version-1 message wire format.

1. Create a focused branch.
2. Add a regression test for behavior changes. Tests must set a temporary
   `AGENT_MAILDIR_ROOT`; never use a real mailbox or Claude settings file.
3. Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v`.
4. Run `agent-maildir doctor` only against temporary test data.
5. Open a pull request describing behavior, compatibility, and validation.

Linux and macOS are supported. Windows changes must address the current
`fcntl` locking dependency rather than silently weakening locking.

## Release

Releases use PyPI Trusted Publishing; never add a PyPI token or password.

1. Bump `project.version` in `pyproject.toml` and `__version__` in
   `src/agent_maildir/__init__.py` to the same `X.Y.Z` value.
2. Update `CHANGELOG.md` and run the full local test and package-install checks.
3. Confirm the `ci.yml` matrix passes.
4. Create the exact tag `vX.Y.Z` and push that tag.
5. `release.yml` calls the reusable CI workflow, verifies that the tag matches
   `pyproject.toml`, builds the sdist and wheel, then publishes through the
   protected `pypi` environment with OIDC.
