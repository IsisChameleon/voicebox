# Task C — Phase 0 timing instrumentation

*Evidence for the success criteria in
[`docs/specs/2026-07-31-fix-plan-execution.md`](../../specs/2026-07-31-fix-plan-execution.md)
§ "Task C — Phase 0 instrumentation (timing only, no behaviour change)".*

Captured 2026-08-01.

## Criteria → evidence

| # | Criterion | Evidence |
|---|---|---|
| 1 | Duration logged at DEBUG for `run_stt`, `analyze_end_of_turn`, `on_user_turn_stopped` | `timing.py:57-106`, `agent.py:376` — live lines below |
| 2 | One stable greppable prefix, duration as a parseable number | `TIMING_PREFIX = "voicebox.timing"`, format `name=<call> secs=<float>` |
| 3 | Output unchanged at the default level; no new frames/awaits/reordering | `test_timing_silent_at_default_level`, `test_timed_stt_passes_frames_through_unchanged` |
| 4 | No vendored pipecat code — subclass or wrap | mixins over `STTService` / `BaseTurnAnalyzer`, `super()` delegation only |

## The lines it emits

Driving one VAD-delimited segment through a minimal pipeline plus one analyzer call, with a
DEBUG sink on stdout:

```
$ uv run python - <<'PY'   # drives _run_one_stt_segment(_TimedStubSTT()) + _TimedStubTurnAnalyzer()
...
PY
voicebox.timing name=run_stt secs=0.051
voicebox.timing name=analyze_end_of_turn secs=0.053
```

The stubs sleep `STT_WORK_SECS = 0.05` and `ANALYZER_WORK_SECS = 0.05` respectively, so the
logged floats are real elapsed time, not placeholders. Grepping a live session log:

```bash
grep -o 'voicebox\.timing name=[a-z_]* secs=[0-9.]*' session.log
```

## Test run

```
$ uv run pytest -q tests/test_timing_instrumentation.py
...                                                                      [100%]
3 passed in 4.95s
```

Whole suite, confirming the instrumentation did not break anything else:

```
$ uv run pytest -q
1 failed, 27 passed in 7.61s
```

The one failure is `tests/test_metrics.py::test_turns_count_matches_utterances` — Task B, in
flight and unrelated to this task. Baseline at branch start was 14 passed; the suite is now 28
tests.

## The hang that had to be fixed first

Both pipeline-driven tests hung indefinitely before this commit (killed at 45 s; a plain
`uv run pytest -q` never returned). Cause: the harness tore the pipeline down with
`await worker.stop()`, which is `BaseWorker.stop()` — it cancels job groups and sets
`_finished_event`, but does **not** end the pipeline run
(`.venv/.../pipecat/workers/base_worker.py:352-363`). The runner task therefore never returned and
`await run_task` blocked forever.

Fix: `await worker.stop_when_done()`, which queues an `EndFrame`
(`.venv/.../pipecat/pipeline/worker.py:648-655`) — the same teardown production uses at
`agent.py:449`. Recorded as a trap in `CLAUDE.md`, since Tasks D–G all need pipeline harnesses.

## Quality gates

```
$ uv run ruff check src/ tests/
All checks passed!

$ uv run pyright src/
  src/voicebox/agent.py:407:42 - error: "start_recording" is not a known attribute of "None"
  src/voicebox/browser_session.py:35:23 - error: Variable not allowed in type expression
  2 errors, 0 warnings, 0 informations
```

Both are **pre-existing, not new**. Verified by running pyright over the committed `agent.py`
(`git show HEAD:src/voicebox/agent.py`), which reports the identical error at line 389 — the same
line, shifted by the +18 lines this task adds:

```
src/voicebox/agent.py:389:42 - error: "start_recording" is not a known attribute of "None"
1 error, 0 warnings, 0 informations
```

The `browser_session.py` error is documented in
[`t-a-startup-fails-loudly.md`](t-a-startup-fails-loudly.md).

`ruff format --check` currently reports `src/voicebox/metrics.py` would be reformatted — that is
Task B's uncommitted file, not this task's.

## Not covered

- **C3 🔴 — the deliverable that matters.** Attributing the observed ~24 s per-turn lag to a
  named call needs one live session at DEBUG against a talkative app on `localhost:3000`. Until
  that runs, the hypothesis that `LocalSmartTurnAnalyzerV3` is responsible remains a hypothesis,
  and the conditional follow-up in Task D (dropping `TurnAnalyzerUserTurnStopStrategy`) stays
  unjustified.
- `on_user_turn_stopped` is instrumented (`agent.py:376`) but has **no test** — the two tested
  call sites are `run_stt` and `analyze_end_of_turn`. It is a plain `with log_duration(...)`
  wrapper around an existing body.
- The timings are measured against stubs that sleep. Nothing here proves the mixins compose
  correctly with the *real* `WhisperSTTService` / `LocalSmartTurnAnalyzerV3` MRO at runtime —
  only that they compose with a subclass of a concrete service
  (`_TimedStubSTT(TimedSTTMixin, _StubSTT)`), which is the same shape.
