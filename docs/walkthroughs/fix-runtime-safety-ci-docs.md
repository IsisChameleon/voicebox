# Runtime safety, CI, and diagram fixes

Status: complete

| Task | Commit | Status | Evidence |
|---|---|---|---|
| T1 — truthful connection state, bounded waits, and dropped barge-ins | `1fa93c7` | ✅ | [T1 evidence](../artefacts/fix-runtime-safety-ci-docs/t1-speech-lifecycle-safety.md) |
| T2 — bounded pre-microphone audio buffer | `1fa93c7` | ✅ | [T2 evidence](../artefacts/fix-runtime-safety-ci-docs/t2-shim-buffer-bound.md) |
| T3 — Python 3.11 test and type-check CI | `1fa93c7`, `b0d5330` | ✅ | [T3 evidence](../artefacts/fix-runtime-safety-ci-docs/t3-ci-contract.md) |
| T4 — current architecture-diagram instructions | `1fa93c7` | ✅ | [T4 evidence](../artefacts/fix-runtime-safety-ci-docs/t4-diagram-contract.md) |

## T1 — truthful connection state, bounded waits, and dropped barge-ins

- Disconnect clears the current-client event in `src/voicebox/agent.py`.
- Plain and polite speech have child-side ceilings in `src/voicebox/timeouts.py`.
- `src/voicebox/server.py` derives its parent deadline from the same budgets.
- An armed trigger emits `tester_barge_in_dropped` if disconnected at fire time.
- Try it: `uv run pytest -q tests/test_agent_surface.py tests/test_server_deadlines.py`.
- Not covered: live browser reload and WebSocket reconnect.

## T2 — bounded pre-microphone audio buffer

- `src/voicebox/shim.js` retains at most three seconds of pre-microphone audio.
- Eviction closes the oldest `AudioData` and increments public diagnostics.
- Try it: `node --test tests/shim_pending_inbound.test.mjs`.
- Not covered: live Chromium memory profiling.

## T3 — Python 3.11 test and type-check CI

- All workflows install Python 3.11.
- The build installs Chromium, then requires Python tests, the Node shim test,
  and Pyright over production source.
- Try it: `uv run pytest -q tests/test_ci_docs_contract.py`.
- Not covered: an Actions-hosted run before push.

## T4 — current architecture-diagram instructions

- `diagrams/index.html` now agrees with the current server and README contracts.
- The contract test rejects the obsolete attach, listen, and stop descriptions.
- Try it: `uv run pytest -q tests/test_ci_docs_contract.py`.
- Not covered: screenshot comparison.

## Try it

```bash
uv run pytest
uv run pyright src/
uv run ruff check
uv run ruff format --check
```
