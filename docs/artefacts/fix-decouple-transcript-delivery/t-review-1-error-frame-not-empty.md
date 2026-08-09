# Review fix 1 — a failed transcription is not an empty segment

*2026-08-09. Commit `602fd85`. Decision: `BUILDLOG.md` D25 (refines D24).*

Evidences no spec criterion — this is a **review finding against the branch diff**, not a planned
phase. It repairs the Phase 2 mechanism described in
[`docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md`](../../specs/2026-08-06-decouple-transcripts-from-turn-closure.md)
§ "Phase 2 — Re-home the empty-transcript signal", whose Verify line ("exactly once per silent
segment") the pre-fix code could violate through a path the spec never considered: a *failed*
decode.

## The defect, read out of pipecat's source

Phase 2 fired the empty signal on "no `TranscriptionFrame` came out", and relied on `except
Exception` to catch failures. But pipecat's Whisper services do not raise on failure — they
**yield an `ErrorFrame`**. Both of them:

```python
# .venv/.../pipecat/services/whisper/stt.py:354-356  (faster-whisper, model missing)
if not self._model:
    yield ErrorFrame("Whisper model not available")
    return
```

```python
# .venv/.../pipecat/services/whisper/stt.py:547-548  (MLX service — the catch-all)
except Exception as e:
    yield ErrorFrame(error=f"Unknown error occurred: {e}")
```

The MLX `except Exception` wraps the *entire* body of `run_stt`, including the
`asyncio.to_thread(mlx_whisper.transcribe, ...)` call. So every failure mode of the decode itself —
the ones most likely to happen in the field — is swallowed there and converted into a yielded frame
that never reaches voicebox's `except`.

Consequence, and why it is not cosmetic: `agent._on_empty_segment` claims one entry from
`_unclaimed_bot_speech_starts`. A failed segment therefore consumed a VAD start it never earned, and
**every later transcript in the session** was stamped with its neighbour's start — the same D10
deque drift that Phase 2 was written to prevent, re-entering through the error door.

## The change

One term added to the condition; the pass-through wrapper already saw every frame
(`src/voicebox/processors/nonblocking_whisper_stt.py:195-231`):

```python
async def counting(source: AsyncGenerator[Frame, None]) -> AsyncGenerator[Frame, None]:
    """Pass every frame through untouched, counting what came out."""
    nonlocal transcripts, errors
    async for frame in source:
        if isinstance(frame, TranscriptionFrame):
            transcripts += 1
        elif isinstance(frame, ErrorFrame):
            errors += 1
        yield frame
...
transcripts = errors = 0
await self.process_generator(counting(super().run_stt(audio)))
if not transcripts and not errors and self.on_empty_segment is not None:
    await self.on_empty_segment()
```

`process_generator` remains the only thing that forwards frames, so pipecat's `ErrorFrame` →
`push_error_frame` dispatch is still not vendored into this repo — the property the Phase 2 artefact
argued for is preserved.

### Behaviour, before and after

| Segment outcome | Before | After |
|---|---|---|
| `TranscriptionFrame` | forwarded, event emitted | unchanged |
| No frames at all (genuine silence) | `transcription_empty: true`, VAD start claimed | unchanged |
| `ErrorFrame` yielded | ❌ `transcription_empty: true`, VAD start **wrongly claimed** | ✅ pipecat error path only; no event, no claim |
| Exception raised | logged, segment dropped, worker survives | unchanged |

## Test run

The regression test drives the real `_Harness` pipeline (`Pipeline([stt, downstream])` on a live
`PipelineWorker`) against a Whisper stand-in that yields **only** an `ErrorFrame` for segment 1 and a
normal transcript for segment 2 — mimicking the `yield ErrorFrame(...)` above rather than asserting
against a mock (`tests/test_nonblocking_stt.py:285-343`).

One correction to a first draft of the test, worth recording because it is easy to get wrong:
`push_error_frame` pushes **UPSTREAM** (`pipecat/processors/frame_processor.py:700`) after firing the
`on_error` event handler (`:688`), so the `ErrorFrame` never reaches a processor placed *downstream*
of the STT. Asserting on `_Downstream` would have passed vacuously for the wrong reason. The
observable hook is the event handler:

```python
stt.add_event_handler("on_error", lambda _proc, frame: errors.append(frame))
```

```
$ uv run pytest tests/test_nonblocking_stt.py -v

tests/test_nonblocking_stt.py::test_speak_not_blocked_by_transcription PASSED [ 10%]
tests/test_nonblocking_stt.py::test_blocking_stt_is_what_this_fixes PASSED [ 20%]
tests/test_nonblocking_stt.py::test_transcripts_preserve_segment_order PASSED [ 30%]
tests/test_nonblocking_stt.py::test_listen_reports_transcription_backlog PASSED [ 40%]
tests/test_nonblocking_stt.py::test_worker_survives_a_failing_segment PASSED [ 50%]
tests/test_nonblocking_stt.py::test_teardown_leaves_no_worker_running PASSED [ 60%]
tests/test_nonblocking_stt.py::test_eager_model_decodes_inside_the_transcribe_call PASSED [ 70%]
tests/test_nonblocking_stt.py::test_empty_segment_signals_once_and_only_when_silent PASSED [ 80%]
tests/test_nonblocking_stt.py::test_error_frame_is_not_an_empty_segment PASSED [ 90%]
tests/test_nonblocking_stt.py::test_empty_segment_signal_is_optional PASSED [100%]
======================== 10 passed, 1 warning in 37.64s ========================
```

The two Phase 2 tests it had to leave alone — `test_empty_segment_signals_once_and_only_when_silent`
(genuine silence still signals, exactly once) and `test_empty_segment_signal_is_optional` (no
callback set must not kill the worker) — both still pass, which is what says the fix narrowed the
condition rather than disabling it.

The D24 consumers of the signal are untouched and green:

```
$ uv run pytest tests/test_stop_drains_stt.py tests/test_transcript_delivery.py -q
8 passed, 2 warnings in 3.40s
```

## Mutation check — the test fails without the change

The production condition reverted to its pre-fix form (`if not transcripts and
self.on_empty_segment is not None`), nothing else touched:

```
$ uv run pytest tests/test_nonblocking_stt.py::test_error_frame_is_not_an_empty_segment -q
FAILED tests/test_nonblocking_stt.py::test_error_frame_is_not_an_empty_segment
1 failed, 1 warning in 3.27s
```

The file was then restored from a copy and the restored condition re-confirmed by grep before
committing:

```
$ grep -n "not transcripts and not errors" src/voicebox/processors/nonblocking_whisper_stt.py
226:                if not transcripts and not errors and self.on_empty_segment is not None:
```

## Whole suite, lint, types

```
$ uv run pytest -q
88 passed, 6 warnings in 61.81s (0:01:01)

$ uv run ruff check src/ tests/
All checks passed!

$ uv run ruff format --check src/ tests/
26 files already formatted

$ uv run pyright src/
  src/voicebox/agent.py:548:42 - error: "start_recording" is not a known attribute of "None" (reportOptionalMemberAccess)
  src/voicebox/browser_session.py:39:23 - error: Variable not allowed in type expression (reportInvalidTypeForm)
2 errors, 0 warnings, 0 informations
```

Both pyright errors are the same two pre-existing ones carried since the Phase 1 artefact. Verified
as pre-existing rather than assumed, by re-running against a stashed working tree:

```
$ git stash -q && uv run pyright src/ | grep -E "^ +/|errors," ; git stash pop -q
  src/voicebox/agent.py:548:42 - error: "start_recording" is not a known attribute of "None" (reportOptionalMemberAccess)
  src/voicebox/browser_session.py:39:23 - error: Variable not allowed in type expression (reportInvalidTypeForm)
2 errors, 0 warnings, 0 informations
```

Identical before and after — the change introduces no new diagnostic.

## Not covered

* **🔴 No live reproduction of a real Whisper `ErrorFrame`.** The regression test stubs `run_stt`.
  Provoking the genuine article means breaking the model at runtime (a corrupt model path, an OOM
  mid-decode); it was not attempted, and no dogfood session ran for this fix. The pipecat source
  quoted above is the evidence that the path exists — the *shape* of the failure is verified from
  the vendored source, the *frequency* is not.
* **The wiring line `stt.on_empty_segment = self._on_empty_segment` (`src/voicebox/agent.py:481`)
  still has no unit test**, unchanged from the Phase 2 artefact's note, for the same reason
  (reaching it loads Whisper, Kokoro and Silero).
* **The `on_error` handler asserted in the test is the pipeline-level one, not a voicebox
  consumer.** voicebox registers no `on_error` handler on the STT in production; the frame goes
  upstream and is logged by pipecat (`frame_processor.py:699`). Whether voicebox *should* surface
  transcription failures to `listen()` is left open — `BUILDLOG.md` D25 records it as rejected for
  now, for want of a consumer.
* **The `except Exception` branch is unchanged and remains covered only by
  `test_worker_survives_a_failing_segment`**, which raises rather than yields — the two failure
  shapes are now tested separately, but no test exercises both in one session.
