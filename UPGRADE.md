# XDSH upgrade runbook

Before changing either side of the pair, read the
[XDSH / DSH compatibility matrix](COMPATIBILITY.md). A package and runtime are
switched together; a floating or untested DSH version is not considered
compatible.

1. Back up the XDSH-owned state root and record the current XDSH/DSH versions.
2. Install the candidate package in a separate virtual environment.
3. Run `xdsh-doctor --mode both --strict --probe-runtime` and the local
   hermetic tests.
4. Run `xdsh-smoke` with a fresh root, workspace, session ID, and `DSH_HOME`.
5. Switch the service only after one task, one multi-turn continuation, one
   structured tool call, and one cancellation pass.
6. Keep the prior package/runtime/image available. If the contract gate fails,
   stop the candidate and restore the prior pair; do not reuse an ambiguous
   task automatically.

This stable release is `xdsh==0.1.1rc2` and accepts only the same DSH release
line `0.1.1-rc.2`; do not substitute another version or launcher. A later DSH runtime must be paired with a
separately versioned XDSH release, isolated state/runtime roots, a fresh
four-case smoke matrix, and an independently reversible deployment.

The adapter contract is `dsh-sdk-jsonrpc-session-events-v1`. A DSH release with
additive optional fields may pass without code changes; a missing required
operation must produce `DSH_SDK_INCOMPATIBLE` and a failed, non-submitted turn.
