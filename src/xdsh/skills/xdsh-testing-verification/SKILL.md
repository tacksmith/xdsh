---
name: xdsh-testing-verification
description: Validate an XDSH change with isolated tests, exact commands, and evidence proportional to its runtime and safety risk.
---

# Testing and verification

- Start with hermetic unit tests and a temporary workspace; never point a test
  at a production Telegram chat, credential, session, or user workspace.
- Add a focused regression for each behavior or failure mode changed. Include
  negative tests for path, credential, schema, resource, and authorization
  boundaries when applicable.
- Run the repository's formatter/linter, strict type checker, tests, and
  compile or packaging check appropriate to the change.
- For a live probe, pin the model/runtime, bound time and tokens, record only
  aggregate outcomes, and keep the route loopback/authenticated.
- Report the exact command, pass/fail result, environment limitation, and any
  unverified production step. Passing a unit test is not evidence of a remote
  deployment.
