# Walkthrough — `generated-utterance-audio-buffer`

**Status: in review.** Pull Request (PR) [#23](https://github.com/IsisChameleon/voicebox/pull/23),
closes [issue #19](https://github.com/IsisChameleon/voicebox/issues/19).

Replaces the 220-line fork-by-copy Kokoro Text-to-Speech (TTS) module with pipecat's stock service
plus a playout buffer, then makes the tests able to see whether that actually works.

*Created late (2026-08-14), after the first four tasks landed — this branch started before the
walkthrough was in place. Tasks 1–2 are reconstructed from their commits and existing artefacts;
tasks 3–4 were written as they landed.*

## Status table

| # | Task | Commits | Evidence | |
|---|---|---|---|---|
| 1 | Stock Kokoro service + `GeneratedUtteranceAudioBuffer`, fork deleted | `05a0bb6`, `75a8b70` | [implementation.md](../artefacts/generated-utterance-audio-buffer/implementation.md) | ✅ |
| 2 | Fix the three defects the migration shipped with (found by `/code-review high`) | `3be9a4c`, `50313ed` | [t-2-review-fixes.md](../artefacts/generated-utterance-audio-buffer/t-2-review-fixes.md) | ✅ |
| 3 | Buffer tests ported to real pipelines; warm-up onto the real service | `4ef5384` | [t-3-real-pipeline-tests-and-smoke.md](../artefacts/generated-utterance-audio-buffer/t-3-real-pipeline-tests-and-smoke.md) | ✅ |
| 4 | `localhost:3000` removed; smoke scripts self-contained and able to fail | `c2fb0fd` | [t-3-real-pipeline-tests-and-smoke.md](../artefacts/generated-utterance-audio-buffer/t-3-real-pipeline-tests-and-smoke.md) | ✅ |

Decisions: [BUILDLOG](../../BUILDLOG.md) D30, D31, D32, D33.

## Task 1 — stock service + playout buffer

- Deleted `src/voicebox/processors/kokoro_tts.py` (220 lines), ~160 of which were verbatim copies
  of pipecat's own downloader, model-file management and language map.
- The voicebox-specific behaviour became two things instead of a fork:
  - `text_aggregation_mode=TextAggregationMode.TOKEN` — one `speak()` is one synthesis context.
  - `GeneratedUtteranceAudioBuffer` (`src/voicebox/processors/generated_utterance_audio_buffer.py`)
    — holds an utterance's audio and releases it as one gap-free span, so synthesis delays do not
    become silence in the synthetic microphone.
- Nova, the in-repo fake app (`tests/eval/fake_app/`), deliberately uses **stock, unbuffered**
  Kokoro: the reference instrument must not share tester-side policy with the system under test
  (D30).

## Task 2 — three defects the green suite did not see

All three lived in base-class behaviour the old tests stubbed away (D31):

- **Premature `TTSStoppedFrame`.** Stock `TTSService` pushes one after `stop_frame_timeout_s`
  (3.0 s default) of silence on an open audio context. Kokoro under TOKEN aggregation synthesizes
  the whole utterance first — measured **19.1 s** for six sentences. Fixed by passing
  `stop_frame_timeout_s=TTS_STOP_FRAME_TIMEOUT_SECS` (120 s).
- **`_tts_pending` decremented twice** per `speak()`, because the observer counted the premature
  stop frame — so `wait_for_playout` could resolve on the wrong utterance.
- **TTS warm-up failed every session** with "Sample rate should be over 0": `sample_rate` stays 0
  until `StartFrame` arrives, and the failure is *yielded* as an `ErrorFrame`, never raised.

## Task 3 — the buffer's tests now run in real pipelines

- `tests/test_generated_utterance_audio_buffer.py` rewritten around
  `pipecat.tests.utils.run_test`: real `Pipeline`, real `PipelineWorker`, **both** directions
  captured. `_CaptureBuffer` and 11 hand-driven `process_frame` calls deleted.
- The old error test fed an `ErrorFrame` **downstream**. Production pushes error frames
  **upstream** (`frame_processor.py:680` → `push_frame(error, UPSTREAM)`), so it passed against
  behaviour that could not execute. The new test uses a processor placed *below* the buffer that
  fails the way production does.
- Withholding is proven by **order inversion** — a marker sent after the audio must arrive before
  it. The final sequences are otherwise identical, buffered or not.
- Warm-up moved onto the real service in `tests/test_generated_utterance_in_pipeline.py`, where a
  real `StartFrame` sets `sample_rate`.
- Every test was **mutation-checked**: four mutations of the production processor, each failing
  exactly the test that names it. A green port is not evidence (D33).
- Corrected a false comment in the processor: it claimed `agent.py` wires `on_error` to
  `discard()`. That wiring was rejected in D32 and does not exist.

## Task 4 — no more `localhost:3000`

- `start_browser_session(url)` is now **required**. voicebox exists to test *your* app, so there is
  no sensible default; the old one pointed at an app that has not existed since PR #18.
- Both smoke scripts needed a *secure origin*, not an app. New `scripts/blank_page_server.py`
  serves one empty page on an ephemeral port, so they depend on nothing being up.
- `scripts/smoke_browser_shim.py` now exits non-zero when no audio reaches the page. It previously
  logged `✗ no inbound audio chunks received` and exited 0.

## Try it

```bash
uv run pytest -q                                   # 113 passed, 1 xfailed
uv run ruff check src/ tests/ scripts/
uv run python scripts/smoke_browser_shim.py        # ~25 s, needs no voice app
```

## Not covered

- **Task 3:** the other five hand-driven test files (`test_nonblocking_stt.py`,
  `test_vad_placement.py`, `test_transcript_delivery.py`, `test_stop_drains_stt.py`, the observer
  half of `test_agent_surface.py`) stay with issue #24.
- **Task 2/3:** the D32 failure contract is still **not implemented** — a failed synthesis plays
  its partial audio — and is pinned as a `strict=True` xfail stating the specification. Filed as
  [issue #25](https://github.com/IsisChameleon/voicebox/issues/25), which needs a design pass
  before code: both obvious fixes were traced and rejected.
- **Task 4:** `scripts/smoke_full_duplex.py` was changed the same way but not run; no live dogfood
  against Nova this round.
- Timings are one WSL2 host with a warm ONNX cache, not a distribution.

## Found on the way — filed, not fixed here

- **Issue [#22](https://github.com/IsisChameleon/voicebox/issues/22) reproduced in-repo.** The
  smoke run shows the audio WebSocket binding ~9 s *after* `start_browser` reports the session
  ready, with the shim retrying meanwhile. That repro previously needed an external voice app; it
  now takes ~25 s and no app.
- **Issue [#24](https://github.com/IsisChameleon/voicebox/issues/24)** — the remaining ports.
- **[IsisChameleon/readme#190](https://github.com/IsisChameleon/readme/issues/190)** — the same
  test-harness problem in the sibling voice app, including a mixin whose contract is a claim about
  pipecat's base class, verified against a fake base class written to satisfy it.
