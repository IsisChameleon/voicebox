# Task E — transcription leaves the frame path

*Evidence for the success criteria in
[`docs/specs/2026-07-31-fix-plan-execution.md`](../../specs/2026-07-31-fix-plan-execution.md)
§ "Task E — Take transcription off the frame path".*

Captured 2026-08-01. Commit `32b1d6c`.

## Criteria → evidence

| # | Criterion | What landed | Evidence |
|---|---|---|---|
| 1 | New `processors/nonblocking_whisper_stt.py` overriding `_handle_user_stopped_speaking` to enqueue and return | **Adapted** — the file exists; it intercepts `run_stt` instead, so pipecat's segment framing is never copied. BUILDLOG D5 | `nonblocking_whisper_stt.py` |
| 2 | One worker, not a pool | one `create_task(self._transcribe_worker())`, guarded against double-start | `test_transcripts_preserve_segment_order` |
| 3 | Frames pushed from the worker in segment order | single FIFO queue, single consumer | `test_transcripts_preserve_segment_order` |
| 4 | Worker starts/stops with the processor lifecycle; no leak, no hang | `start()` / `stop()` / `cancel()`; `_worker` is `None` after teardown | `test_teardown_leaves_no_worker_running` |
| 5 | Wired in `_create_stt_service` for the non-Darwin path; Darwin is the implementer's call | wired for **both** — BUILDLOG D6 | `agent.py:_create_stt_service` |
| 6 | `PLAYOUT_TIMEOUT_SECS` 120 → 30; expiry returns `{queued, played: false, reason}`; `server.py` deadline drops in step | done; deadline split (see below) | `test_playout_timeout_returns_diagnostic`, `test_playout_timeout_does_not_raise` |
| 7 | `listen()` gains `transcription_lag_secs`; `events`/`cursor` untouched | done | `test_listen_envelope_reports_transcription_lag` |

## E1 — the measurement

A 4 s transcription is put in flight; 0.5 s later a `speak()` frame is queued behind it. How
long until it reaches the transport:

```
Transcription takes 4.0s. speak() queued 0.5s into it:
  non-blocking worker : LLMTextFrame reached the transport in   96.7 ms
  inline (pipecat)    : LLMTextFrame reached the transport in 3503.5 ms
```

Both rows come from the same harness; the inline row is the negative control
(`test_blocking_stt_is_what_this_fixes`), which is what makes the first row attributable to the
mixin rather than to the test being easy. 3503 ms is the remainder of the transcription — the
frame was waiting for Whisper to finish, exactly as the field report described.

## The composition is load-bearing

`run_stt` means "hand off" at the front of the MRO and "transcribe" further along it, so the
order of the bases is asserted rather than assumed:

```
$ uv run python -c "..."
_NonBlockingWhisperSTTService -> NonBlockingSegmentedSTT -> TimedSTTMixin ->
    WhisperSTTService -> SegmentedSTTService -> STTService -> AIService
frame-path run_stt owner : NonBlockingSegmentedSTT.run_stt
worker calls             : TimedSTTMixin.run_stt
```

The second line is the frame task being freed; the third is Task C's timing still wrapping the
real Whisper call, now inside the worker.

## Criterion 6 — the deadline split

The spec says `server.py`'s `deadline = 150.0` "drops in step". It covered two different waits:

* `wait_for_playout` → **60 s**. It must outlive the agent's own 30 s `PLAYOUT_TIMEOUT_SECS`, so
  the caller receives the agent's diagnosis instead of an IPC timeout that says less.
* `wait_for_turn` → **150 s, unchanged.** It waits for the app bot to stop talking and has no
  agent-side timeout at all; the app bot decides when that is. Dropping it would have converted
  a long bot utterance into a spurious failure.

## Test run

```
$ uv run pytest -q tests/test_nonblocking_stt.py
......                                                                   [100%]
6 passed in 33.59s

$ uv run pytest -q tests/test_agent_surface.py
....                                                                     [100%]
4 passed in 3.76s

$ uv run pytest -q
44 passed, 3 warnings in 49.50s

$ uv run ruff check src/ tests/
All checks passed!

$ uv run ruff format --check src/ tests/
19 files already formatted
```

`uv run pyright src/` is back to the 2-error baseline (`agent.py` `start_recording` on an
`Optional`, `browser_session.py:35`). Three `# type: ignore`s were needed and each is explained
at its site: two for pipecat annotating the abstract `STTService.run_stt` as a coroutine
returning a generator rather than as an async generator, one per Whisper class for `_settings`
being re-declared with a service-specific `Settings` type.

## Not covered

* **E5 🔴 (live-only)** — that barge-in audio starts within ~2 s (Kokoro synthesis) rather than
  after the transcript lands. Needs a real app.
* **The MLX path is wired but never exercised.** This machine is Linux and no test constructs
  the MLX service (BUILDLOG D6).
* **Real Whisper never runs in these tests.** The stub takes a fixed 4 s sleep. What is proven
  is that the frame task is free while transcription runs, not anything about Whisper's speed —
  which the triage already measured at 0.40x realtime warm.
* **Nothing here drains the queue at teardown.** A transcription still in flight when `stop()`
  is called is cancelled, so its transcript never reaches `events.json`. That is Task F, which
  is why F immediately follows E.
* **A failed segment is dropped with a log line, not surfaced as an event.** The worker survives
  it (`test_worker_survives_a_failing_segment`) but a reader of `events.json` sees only a
  missing transcript. Task F's `transcription_empty` flag covers the empty-result case, not this
  one.
