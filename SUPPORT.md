# XDSH support

## Supported baseline

The stable public line supports only XDSH `0.1.1-rc.2` with DSH
`0.1.1-rc.2` and `deepseek-harness-sdk==0.1.1rc1`. A different DSH runtime is
version drift, not a supported in-place upgrade.

## Before opening an issue

Run these read-only checks and include their redacted status, never their
environment values:

```bash
xdsh-doctor --mode both --strict --json
xdsh --check
python -m xdsh.ops.rate_limit_probe
```

State the operating system, Python/Node versions, XDSH and DSH versions, failing
command, and smallest reproducible sequence. Remove prompts, answers, local
paths, chat IDs, tokens, API keys, provider payloads, and environment files.

Use a normal issue for reproducible bugs and documentation gaps. Use the
repository's private security-advisory channel for credential exposure,
authentication bypass, path escape, or remote-code-execution concerns. General
provider outages and DSH defects should be reproduced outside XDSH and reported
to the component that owns them.
