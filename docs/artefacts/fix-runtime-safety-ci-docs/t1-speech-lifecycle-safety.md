# T1 — Speech lifecycle safety evidence

Success criteria: prove the current branch refuses disconnected speech, bounds
turn waits, keeps parent and child deadlines coherent, and reports dropped
armed barge-ins.

## Red

- Command, before production edits:

```bash
UV_CACHE_DIR=/tmp/voicebox-uv-cache uv run pytest -q \
  tests/test_agent_surface.py tests/test_server_deadlines.py \
  -k 'speak_without_client or disconnect_clears or wait_for_turn_times_out or armed_speak_drops or wait_for_turn_child_timeout'
```

- Result: `5 failed`.
- Failures named missing connection/turn timeout constants, missing disconnect
  state clearing, and missing `tester_barge_in_dropped` behavior.

## Green

- Same focused command: `5 passed`.
- Affected files: `26 passed`.
- Full suite after integration: `106 passed, 3 warnings in 63.88s`.
- Production type check: `0 errors, 0 warnings, 0 informations`.

## Not covered

- No live browser reload or WebSocket reconnect probe.
- Unit tests exercise the real agent methods with a fake pipeline worker.
