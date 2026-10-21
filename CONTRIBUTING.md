# Contributing to XDSH

XDSH welcomes focused fixes that preserve its runner-neutral product boundary.
The stable branch supports exactly XDSH `0.1.1-rc.2` with DSH `0.1.1-rc.2`;
runtime upgrades belong in a separately versioned compatibility line.

## Development setup

```bash
uv sync --locked --all-groups --all-extras --python 3.12
.venv/bin/python -m pytest -q tests/unit
.venv/bin/ruff check src tests
.venv/bin/mypy --python-version 3.12 src tests
.venv/bin/python -m compileall -q src tests
```

All extras enable the gateway, channel SDK and HDF5 checks instead of skipping them.
Match mypy's Python target to the test environment: native dependency stubs may
use that interpreter's syntax. Public CI also checks Python 3.10 separately to
enforce the minimum supported source version.
The public repository contains the credential-free core contract suite used by
its CI. Release maintainers run a larger isolated suite containing deployment
and adversarial fixtures before building an official candidate. Live provider,
Telegram, and host tests are always opt-in and must use disposable XDSH state.

## Change rules

- Keep DSH-specific behavior behind `DshRunner`; do not add a custom DSH plugin.
- Keep credentials in environment variables and use placeholders in tests and docs.
- Bind services to loopback unless an authenticated, documented proxy owns ingress.
- Add a regression test for behavior changes and keep diagnostics bounded/redacted.
- Name the compatibility or feature-migration row affected in the pull request.

Run `xdsh-release export --destination /tmp/xdsh-public` before submitting a
release-related change. The export must pass `xdsh-release verify
/tmp/xdsh-public --strict` without weakening its credential or path scanner.

Security vulnerabilities should be reported privately as described in
[`SECURITY.md`](SECURITY.md), not opened as public issues.
