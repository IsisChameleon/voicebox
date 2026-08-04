# Task I — Shim diagnostics become artifacts (`shim.log` + `shim_diag.json`)

*2026-08-04. Evidence for the scenario in
`docs/architecture/deltas/shim-diagnostics-artifacts.md` §S1 (decision: `BUILDLOG.md` D22):
a broken or degraded audio tap is diagnosable from the artifact set alone — the shim's
tagged console lines land in `record_dir/shim.log` as they happen, teardown snapshots
`window.__voiceShim` to `record_dir/shim_diag.json`, and `stop()` returns both paths even
when the pipecat child is wedged.*

Code commit: `1551a90`.

## Live proof — real Chromium, no app, no audio server

`start_browser(url="data:...", record_dir="temp/d22-shim-artifacts", audio_ws_url="ws://localhost:9391")`
with nothing listening on :9391, then `stop_browser()` two seconds later:

```
started: {'cdp_endpoint': 'http://localhost:9336', 'audio_ws_url': 'ws://localhost:9391',
          'attach_hint': 'playwright-cli attach --cdp http://localhost:9336'}
stop_browser -> {'shim_log': '/home/isischameleon/src/voicebox/temp/d22-shim-artifacts/shim.log',
                 'shim_diag': '/home/isischameleon/src/voicebox/temp/d22-shim-artifacts/shim_diag.json'}
```

`shim.log` — the install notes AND the error channel (the WS failures are `recordError`
lines, i.e. exactly the class of event that was previously invisible outside CDP):

```
1785811013.098 [log] [voice-shim] skipping getUserMedia hook (insecure context or missing WebCodecs)
1785811013.098 [log] [voice-shim] installed. {micHook: false, pcHook: true, audioWs: ws://localhost:9391}
1785811013.102 [warning] [voice-shim] WS error event: [object Event]
1785811014.103 [warning] [voice-shim] WS error event: [object Event]
1785811015.105 [warning] [voice-shim] WS error event: [object Event]
```

`shim_diag.json` — full final snapshot, getters included, errors array carrying the same
three failures:

```json
{
  "installed": true,
  "micHookInstalled": false,
  "pcHookInstalled": true,
  "wsReady": false,
  "inboundChunks": 0,
  "outboundChunks": 0,
  "audioWsUrl": "ws://localhost:9391",
  "pcCount": 0,
  "audioTrackCount": 0,
  "micTrackCount": 0,
  "outboundSampleRate": null,
  "outboundNumChannels": null,
  "outboundFormat": null,
  "perTrackBytes": {},
  "errors": [
    "WS error event: [object Event]",
    "WS error event: [object Event]",
    "WS error event: [object Event]"
  ],
  "hasMediaDevices": false,
  "hasWebCodecs": true
}
```

## Test suite

New tests (5): live shim-artifact round-trip + no-record_dir null path
(`tests/test_browser_session.py`), parent-side merge including the wedged-pipecat and
no-artifacts cases (`tests/test_server_stop_artifacts.py`).

```
$ uv run pytest tests/test_browser_session.py tests/test_server_stop_artifacts.py -q
........                                                                 [100%]
8 passed in 3.76s

$ uv run pytest -q
81 passed, 5 warnings in 58.67s
```

Lint/format/types: `ruff check` — All checks passed; `ruff format --check` — 24 files
already formatted; `pyright src/` — 2 errors, both verified pre-existing on HEAD before
this change (`git stash` → same 2 errors: `agent.py:534` optional-member, the
`multiprocessing.Event` annotation in `browser_session.py`).

## Not covered

- **A real WebRTC session's `shim.log`** (track tee lines, `perTrackBytes` growth) — 🔴
  live-only, needs a voice app on `localhost:3000`; next dogfood session should confirm the
  two files appear alongside the pipecat artifacts in a full `record_dir`.
- **The `snapshot_error` path** (`_dump_shim_diag` against a crashed/unreachable page) —
  exercised by code inspection only; not simulated.
- **Multi-navigation sessions**: the claim that `shim.log` survives navigations (the reason
  console capture was chosen over `__voiceShim` polling, D22) follows from
  `page.on("console")` semantics; not demonstrated with an actual mid-session navigation.
- The MCP-tool-level flow (`start_browser_session(record_dir=...)` → `stop()`) is covered
  at the seam (`server.stop()` merge tests with `stop_browser` stubbed), not end-to-end
  through FastMCP.
