# Phase 2 — the empty-transcript signal moves to the STT worker

*2026-08-06. Commit `b836f4f`. Decision: `BUILDLOG.md` D24.*

Evidences § "Phase 2 — Re-home the empty-transcript signal" (including its **Decision to settle
before coding** and its **Verify** line) in
[`docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md`](../../specs/2026-08-06-decouple-transcripts-from-turn-closure.md),
and scenario 3 ("silent segment") from § "Scenarios".

## Why the signal had to move at all

Whisper yields **no frame at all** for a segment it recovers no text from — the yield is guarded:

```python
# .venv/.../pipecat/services/whisper/stt.py:377-386
if text:
    await self._handle_transcription(text, True, language)
    logger.debug(f"Transcription: [{text}]")
    yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), language)
```

So after Phase 1 there is nothing for the observer to see, and the Task F event vanished. That was
the readability cost the spec anticipated. It also had a correctness cost the spec's Phase 1 note
predicted: `_emit_app_bot_transcript` claims one entry from `_unclaimed_bot_speech_starts` per call,
so a silent segment that emits nothing leaves its VAD start unclaimed and **every later transcript
claims a neighbour's start** — the D10 deque drifts by one for the rest of the session.

## The decision the spec asked to settle

Taken as recommended: an optional `on_empty_segment` callback attribute on
`NonBlockingSegmentedSTT` (`src/voicebox/processors/nonblocking_whisper_stt.py:116-123`), set by
`agent.py` in `start()`. Rejected alternative (a custom `EmptySegmentFrame`) not built — recorded
in `BUILDLOG.md` D24.

One refinement over the spec's wording. The spec said `_transcribe_worker` should "iterate the
generator itself and count `TranscriptionFrame`s before forwarding". Iterating directly would mean
re-implementing `process_generator`'s dispatch (`ErrorFrame` → `push_error_frame`, else
`push_frame`) inside this repo, which the module docstring exists to avoid. Instead the generator is
wrapped by a pass-through that only counts, and `process_generator` stays the only thing that
forwards frames:

```python
async def counting(source: AsyncGenerator[Frame, None]) -> AsyncGenerator[Frame, None]:
    """Pass every frame through untouched, counting the transcripts."""
    nonlocal transcripts
    async for frame in source:
        if isinstance(frame, TranscriptionFrame):
            transcripts += 1
        yield frame
...
transcripts = 0
await self.process_generator(counting(super().run_stt(audio)))
if not transcripts and self.on_empty_segment is not None:
    await self.on_empty_segment()
```

Same observable behaviour, no vendored pipecat dispatch.

## Test run

The new STT-side tests drive a real pipeline (`_Harness`: `Pipeline([stt, downstream])` on a live
`PipelineWorker`) with a Whisper stand-in that returns without yielding for chosen segments —
mimicking the `if text:` guard rather than asserting against a mock.

```
$ uv run pytest tests/test_nonblocking_stt.py::test_empty_segment_signals_once_and_only_when_silent \
    tests/test_nonblocking_stt.py::test_empty_segment_signal_is_optional \
    tests/test_stop_drains_stt.py -v

tests/test_nonblocking_stt.py::test_empty_segment_signals_once_and_only_when_silent PASSED [ 11%]
tests/test_nonblocking_stt.py::test_empty_segment_signal_is_optional PASSED [ 22%]
tests/test_stop_drains_stt.py::test_pending_transcript_reaches_artifacts PASSED [ 33%]
tests/test_stop_drains_stt.py::test_empty_transcription_still_emits_event PASSED [ 44%]
tests/test_stop_drains_stt.py::test_nonempty_transcription_is_not_flagged PASSED [ 55%]
tests/test_stop_drains_stt.py::test_empty_transcript_still_claims_a_vad_start PASSED [ 66%]
tests/test_stop_drains_stt.py::test_stop_bounded_when_drain_stalls PASSED [ 77%]
tests/test_stop_drains_stt.py::test_drain_completes_when_queue_empties PASSED [ 88%]
tests/test_stop_drains_stt.py::test_drain_budget_scales_with_backlog PASSED [100%]
============================== 9 passed in 6.06s ===============================
```

`test_empty_segment_signals_once_and_only_when_silent` feeds three segments with the second silent
and asserts both halves of the spec's Verify line: the downstream sees `["segment-1", "segment-3"]`
and the callback fired **exactly once**. Once, not "at least once", is the assertion that protects
the deque.

The two `test_stop_drains_stt.py` empty-transcript tests were re-pointed from
`_emit_app_bot_transcript("")` to `agent._on_empty_segment()`, so they now exercise the production
entry point rather than the helper — closing the gap the Phase 1 artefact flagged.
`test_empty_transcript_still_claims_a_vad_start` is the deque-drift regression test, and it goes
through the new path.

Whole suite, lint, types:

```
$ uv run ruff check src/ tests/
All checks passed!

$ uv run pyright src/
  src/voicebox/agent.py:557:42 - error: "start_recording" is not a known attribute of "None" (reportOptionalMemberAccess)
  src/voicebox/browser_session.py:39:23 - error: Variable not allowed in type expression (reportInvalidTypeForm)
2 errors, 0 warnings, 0 informations

$ uv run pytest -q
87 passed in 59.40s
```

Both pyright errors are the same two pre-existing ones recorded in the Phase 1 artefact (the
`agent.py` line number moved with the new method; the diagnostic is unchanged).

## Mutation check — the test fails without the change

Deleting only the two-line signal from `_transcribe_worker`:

```
$ uv run pytest tests/test_nonblocking_stt.py -q
FAILED tests/test_nonblocking_stt.py::test_empty_segment_signals_once_and_only_when_silent
1 failed, 8 passed in 35.83s
```

The file was restored from a copy and `git diff --stat` re-confirmed the 32/3 line change before
committing.

## Not covered

* **The wiring line itself — `stt.on_empty_segment = self._on_empty_segment` in `start()`
  (`src/voicebox/agent.py:481`) — has no unit test.** Both sides are tested (the worker calls its
  callback; `_on_empty_segment` emits the flagged event), but the one line joining them is not,
  because reaching it means running `start()`, which loads Whisper, Kokoro and Silero and needs a
  real transport. It is exercised by the Phase 3 live dogfood session, where a silent app-bot
  segment must show `transcription_empty: true` in `listen()`.
* **🔴 Scenario 3 end-to-end** (a genuinely silent app-bot segment produced by real audio, not a
  stubbed `run_stt`) is live-only for the same reason.
* **Ordering between an empty signal and a neighbouring transcript** is guaranteed by the worker
  being single and serial, and by observers being awaited inside `push_frame`
  (`pipecat/processors/frame_processor.py:881-901`) — asserted only indirectly, via the
  once-per-silent-segment count and the deque test.
