# Delta: shim diagnostics become artifacts (`shim.log` + `shim_diag.json`)

Status: in flight on `fix/audio-path-and-reporting` (D22). Closes the "shim runtime errors
are invisible outside CDP" row in `docs/architecture/4plus1.md` § Gaps.

## Scenarios

### S1 (delta-local): a broken or degraded audio tap is diagnosable from the artifact set alone

Given a session started with `record_dir`, when the in-page shim logs anything through its
tagged console channel (install notes, WS drops, tap errors — `recordError` always goes via
`console.warn('[voice-shim]', ...)`), then `record_dir/shim.log` holds a timestamped line per
message; and when the session stops, `record_dir/shim_diag.json` holds the final
`window.__voiceShim` snapshot (counters, per-track bytes, errors), and `stop()`'s artifacts
dict carries both paths — even when the pipecat child's graceful stop failed.

| # | Hop | View | Evidence |
|---|-----|------|----------|
| 1 | `start_browser_session(record_dir=...)` forwards the dir to the browser child (previously only the pipecat child got it) | development | `src/voicebox/server.py` `start_browser_session` |
| 2 | Browser child subscribes `page.on("console")`, filters on the `[voice-shim]` tag, appends to `shim.log` | process | `src/voicebox/browser_session.py` `_capture_shim_console` |
| 3 | Shim emits tagged lines from install, WS lifecycle, and every `recordError` | logical | `src/voicebox/shim.js:66-70` (recordError → console.warn) |
| 4 | On `stop_event`, before `context.close()`, the child snapshots `window.__voiceShim` to `shim_diag.json` (an unreachable page yields `{"snapshot_error": ...}`, never a missing file) | process | `src/voicebox/browser_session.py` `_dump_shim_diag` |
| 5 | `stop_browser()` returns the paths of whichever files exist; `server.stop()` merges them into `artifacts` in the `finally`, independent of the pipecat stop outcome | logical | `src/voicebox/browser_session.py` `stop_browser`, `src/voicebox/server.py` `stop` |

## View impact

| View | Changed? | What |
|------|----------|------|
| Logical | YES | Two new artifacts in the report set; `stop_browser()` gains a return value (shim artifact paths) |
| Process | YES | Browser child gains a console-event subscriber and a teardown snapshot step (before `context.close()`) |
| Development | YES | `start_browser` signature gains `record_dir`; new tests in `tests/test_browser_session.py` + `tests/test_server_stop_artifacts.py` |
| Physical | no | Same three processes, same ports; two more files under `record_dir` |

## Invariants check

- I11 (artifact ordering) untouched — these files are written by the *browser* child; the
  pipecat child's dump path is unchanged.
- I12 (teardown ordering) extended, not violated: the snapshot runs after the park loop
  exits and strictly before `context.close()`.
- New rule worth recording at fold time: **console capture, not `__voiceShim` polling, is
  the durable error channel** — the diag object is re-created on every navigation, so a
  snapshot only describes the final document.

## Tests derived from the scenario

| Case | Test |
|---|---|
| Tagged console lines land in `shim.log`; final snapshot lands in `shim_diag.json`; `stop_browser()` returns the paths (live headless Chromium, `data:` URL) | `tests/test_browser_session.py::test_shim_artifacts_written` |
| No `record_dir` → no files, `stop_browser()` returns `None` | `tests/test_browser_session.py::test_stop_browser_returns_none_without_record_dir` |
| `server.stop()` merges shim paths with the pipecat child's artifacts | `tests/test_server_stop_artifacts.py::test_stop_merges_shim_artifacts` |
| Shim paths survive a wedged pipecat child (send_command raises) | `tests/test_server_stop_artifacts.py::test_shim_artifacts_survive_failed_pipecat_stop` |

## Fold plan

Update core `4plus1.md`: delete the shim-error Gaps row, add the two artifacts to the
Physical-view artifact row and S4 hop 8, add the console-channel invariant note.
