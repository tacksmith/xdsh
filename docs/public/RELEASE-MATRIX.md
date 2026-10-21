# Public release matrix

The matrix separates deterministic package checks from live provider checks.
Live checks never run as part of a normal install and must use a fresh state
root, workspace, session ID, and DSH home.

| Tier | Checks | Release rule |
| --- | --- | --- |
| Package | sdist/wheel build, archive verifier (duplicate/path/symlink, bounded uncompressed-size, credential-like text, and machine-path checks), 16 Skill resources, Cordis/runtime manifest, multilingual README/compatibility/security/container/channel/WSL docs, public CI/contribution/support policy, Apache-2.0 `LICENSE`/`NOTICE`, `python -m xdsh`, setup/doctor/release/config/QQ entry points | Required on every candidate |
| Hermetic | Full maintainer unit/integration tests plus the credential-free contract profile recorded in `PUBLIC_RELEASE.json` (including bounded stage/artifact checks), runner-neutral channel protocol tests for Discord/WhatsApp/DingTalk/Feishu/WeChat, authenticated loopback settings UI, router de-duplication, bounded HTTP acknowledgement, synthetic typed-429 probe, exact RC2 tool-call liveness, simulated 48-hour active-run protection, Ruff, strict mypy, compileall, shell syntax, lock consistency, external-security negatives | Required on every candidate |
| Local live | One task, multi-turn, structured tool, cancellation, guard/partial output, slow-cold-start tolerance, native compaction pressure | Required before a runtime upgrade |
| Authorized-host candidate | Same model and LiteLLM route, confined Docker execution, Telegram health/menu, exact DSH `0.1.1-rc.2`, rollback image | Required before cutover; writes only `~/.xdsh` on the explicitly authorized host |
| Publication | Explicit license, clean export audit, independent credential review, install from sdist in a fresh environment | Required before registry upload |

The aligned XDSH/DSH `0.1.1-rc.2` pairing is the only supported stable baseline,
documented in [`RC2-BASELINE.md`](RC2-BASELINE.md) and
`rc2-baseline.json`. A later DSH line requires a separately versioned XDSH
release, runtime home, image tag, smoke root, and publication matrix.
Maintainers keep a verified aligned wheel/sdist copy and the former
`0.1.0a0-rc2` evidence as immutable records outside the public source bundle;
neither is used as an implicit deployment input.

Run the package portion from a checkout:

```bash
# Run these maintainer commands from the XDSH source checkout.
python -m pytest -q
ruff check .
mypy --python-version 3.12 src tests
python -m compileall -q src tests
xdsh-release export --destination /tmp/xdsh-public
xdsh-release audit /tmp/xdsh-public
xdsh-release verify /tmp/xdsh-public
uv build --out-dir /tmp/xdsh-public-dist /tmp/xdsh-public
python scripts/verify_public_artifacts.py /tmp/xdsh-public-dist
python scripts/public_install_matrix.py /tmp/xdsh-public-dist
```

`public_install_matrix.py` creates a separate virtual environment for each
archive, checks every public entry point, runs the read-only doctor against a
stub that reports DSH `0.1.1-rc.2`, confirms version drift is surfaced, and
asserts that setup never creates a reserved legacy root. It also checks the
`xdsh-channels`, `xdsh-channel-serve`, and declared `xdsh-config`/`xdsh-qq` entry points
and verifies all generated environment files are owner-only. The matrix also
creates a synthetic DSH
profile and executes the installed `/plugins list` read path, while keeping the
manager read-only and never invoking a package manager. It uses no gateway,
Telegram, DSH task, or production state. Normal CI resolves the pinned SDK;
for an entirely cached run, add `--offline --no-deps` and provide compatible
`--dependency-path` values for the SDK and build backend.
The frozen RC2 command set is enforced independently of later additions; newer
operational commands such as `xdsh-canary` are checked when the installed
artifact declares them in its own metadata. This keeps historical, hash-locked
archives verifiable without weakening checks for current packages.
The doctor also recognizes the official rc.2 JSON-RPC launcher, whose
`--version` flag intentionally prints usage and exits non-zero, by checking its
adjacent package manifest rather than weakening arbitrary failure handling.
The two verifier scripts intentionally stay outside the public export; they
are release-maintainer tooling, not runtime dependencies.

The public CI matrix is configured for Python 3.10, 3.11, 3.12, and 3.13.
Configuration is not execution evidence: actual locally verified combinations
and remaining platform boundaries are recorded in the dated release notes.
The maintainer-only `xdsh-canary` command is excluded from this fixed-RC2 public
profile. The exporter is intentionally not a deployment tool. It does not copy
`HANDOFF.md`, host-specific Docker build inputs, benchmark answers, runtime
state, credentials, private caches, or maintainer-only tests by default. It
does copy the explicit `core-contracts-v1` public test allowlist, `uv.lock`, and
the public CI workflow; every copied file passes the same credential/path scan.
The full maintainer suite can be requested only with `--include-tests` and must
still pass that scan.
The public sdist and wheel are built from that clean export, so repository test
fixtures are not shipped in the normal install artifact.
The source checkout's sdist target also uses an explicit allowlist; the archive
verifier rejects build caches, deployment inputs, and other private paths if they
are accidentally reintroduced.
The exporter copies the selected Apache-2.0 `LICENSE` and project `NOTICE`
automatically. `xdsh-release audit --strict` is the final publication gate; it
must pass before registry upload.

The WSL installer is checked separately with `bash -n` and a no-write dry run.
It installs only the DSH `0.1.1-rc.2` JSON-RPC demo pair under the XDSH-owned
`~/.xdsh/dsh-npm` prefix and rejects floating or different versions. It
installs `xdsh[channels]` so a clean environment contains the optional
cryptography and pinned official QQ SDK dependencies. It never performs a global npm install or touches an
existing DSH installation. A real WSL run on the explicitly authorized test
host remains a host validation gate; it must use a fresh `~/.xdsh` root and
separate provider credentials.

The installer also writes an owner-only `~/.xdsh/bin/dsh` wrapper pinned to the
validated Linux Node binary. This makes a direct DSH version check independent
of a WSL shell's inherited Windows `node.exe` PATH without changing system
startup files.
