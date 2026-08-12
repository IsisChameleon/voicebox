# T2 — Shim buffer bound evidence

Success criteria: prove pre-microphone audio has a duration bound, evicts the
oldest chunks, closes discarded `AudioData`, and exposes diagnostics.

## Red

- Command, before the shim edit:

```bash
node --test tests/shim_pending_inbound.test.mjs
```

- Result: `1 failed`.
- The unchanged shim exposed no maximum-frame diagnostic and retained all seven
  one-second chunks.

## Green

- Same command: `1 passed`.
- Seven one-second chunks enter the shim.
- Four oldest chunks are closed and counted as dropped.
- Three chunks, or 144,000 frames at 48 kilohertz, reach the synthetic mic.
- `node --check src/voicebox/shim.js`: passed.

## Not covered

- The three-second policy has prior live-browser evidence from stale PR #10;
  this branch verifies it with a deterministic Web API harness.
- No real Chromium memory profile was captured on this branch.
