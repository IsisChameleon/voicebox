# Review fixes — evidence

Evidences the verification bar in [issue #19](https://github.com/IsisChameleon/voicebox/issues/19):

> `uv run python scripts/smoke_browser_shim.py` plus one live dogfood against the fake app
> (`tests/eval/fake_app/`) with `record_dir` set — confirm `speak(wait_for_playout=True)` resolves
> at real audio end, one `tester_speech_started/stopped` pair per `speak()`, no mid-utterance
> turn-taking by the app bot. Unit tests cannot see this class of bug.

The first implementation (`05a0bb6`) landed with that bar unmet. `/code-review high` found three
defects; all three were confirmed against installed pipecat 1.3.0 before any fix was written.
Decisions in [BUILDLOG D31](../../../BUILDLOG.md).

## 1. Premature `TTSStoppedFrame` (root cause of the main defect)

Probe: the real `_create_tts_service()` through `Pipeline([tts, GeneratedUtteranceAudioBuffer(), sink])`.

**Before** — three sentences:

```text
  0.05s  TTSStartedFrame
  3.05s  TTSStoppedFrame      <- stop_frame_timeout_s=3.0 fired, no audio yet
  4.21s  TTSAudioRawFrame     <- arrives with _buffering already False -> unbuffered
  4.21s  TTSStoppedFrame      <- second stop; observer counts both
```

**After** — same text, then a six-sentence utterance that genuinely yields two chunks:

```text
  0.07s  TTSStartedFrame
  3.48s  TTSAudioRawFrame
  3.48s  TTSStoppedFrame

  0.08s  TTSStartedFrame
 19.07s  TTSAudioRawFrame     <- 19 s to first chunk; both chunks released
 19.07s  TTSAudioRawFrame        back-to-back by the buffer
 19.07s  TTSStoppedFrame      <- exactly one
```

19.1 s to first chunk against a 3.0 s default is why this was never marginal.

## 2. Unreachable `ErrorFrame` discard

`TTSService` reports failure via `push_error_frame`, which pushes **upstream**:

```text
.venv/.../pipecat/processors/frame_processor.py:699
    await self.push_frame(error, FrameDirection.UPSTREAM)
```

Upstream of TTS is the STT, not the buffer. The D30 test fed the frame downstream, so it passed
against behaviour that could not run. Now checked before the direction shortcut, and the test
pushes it upstream.

## 3. Warm-up failed every session

Probe on the exact production path:

```text
sample_rate before StartFrame = 0
frames: [('ErrorFrame', 'Unknown error occurred: Sample rate should be over 0')]
```

Yielded, not raised, and swallowed by `async for … pass`.

## Regression tests actually fail without the fixes

Source reverted to the buggy `HEAD`, new tests run:

```text
FAILED tests/test_generated_utterance_audio_buffer.py::test_synthesis_error_discards_a_partial_generated_utterance
FAILED tests/test_generated_utterance_audio_buffer.py::test_warm_up_waits_for_the_pipeline_to_set_a_sample_rate
2 failed, 4 passed
```

(`test_tts_context_outlives_synthesis_of_a_long_utterance` cannot run against the old source at
all — `TTS_STOP_FRAME_TIMEOUT_SECS` does not exist there.)

## Live dogfood — the bar itself

Nova in key-free scripted mode; `scripts/dogfood_generated_utterance.py`; `record_dir=temp/dogfood`.

```text
speak returned after 43.9s: {'queued': True, 'played': True,
  'started_at': 1786593697.13, 'finished_at': 1786593721.89, 'interrupted': False}

  1786593697.13  tester_speech_started
  1786593697.47  app_bot_transcript                trivia.
  1786593721.20  app_bot_transcript                First question. Which planet ... has the most moons?
  1786593721.89  tester_speech_stopped

===== CONTRACTS =====
  played                        : True
  tester_speech_started count   : 1  (expect 1)
  tester_speech_stopped count   : 1  (expect 1)
  contiguous span               : 24.8s
  app bot turns mid-utterance   : 0  (expect 0)
```

Warm-up in the same session — synthesized immediately after pipeline start, no warning:

```text
$ grep -c "warm-up" temp/dogfood/agent-debug.log
0
14:01:11.996 | DEBUG | kokoro.tts:run_tts:213 - KokoroTTSService#0: Generating TTS [Ready.]
```

Recorded tester audio (`temp/dogfood/kokoro_voice.wav`), 20 ms RMS windows:

```text
sample_rate=48000 duration=56.9s
speech spans 24.04s .. 48.68s
longest internal silence: 0.60s
top 5 silences: [0.6, 0.58, 0.56, 0.48, 0.46]
```

0.60 s is a sentence pause Kokoro renders, not a synthesis stall; the regression this feature
exists to prevent produced 1.6–4.2 s of dead air.

## Suite

```text
uv run pytest -q                          → 113 passed, 3 warnings in 61.42s
uv run ruff check src/ tests/ scripts/    → All checks passed!
uv run ruff format --check                → 37 files already formatted
```

## Not covered

- **`scripts/smoke_browser_shim.py` still not run.** It hard-codes `http://localhost:3000`, which
  nothing in this repo serves. The dogfood exercises the same audio path against a real app and
  is the stronger of the two, but the smoke script's specific assertions are unverified.
- **Barge-in against a buffered utterance is untested live.** The interruption discard has a
  pipeline-level unit test (`test_interruption_discards_generated_audio_that_has_not_reached_playout`)
  but no dogfood run drove `speak(when=...)` into a mid-buffer interruption.
- **Synthesis failure is untested live.** The `ErrorFrame` path is unit-tested in the production
  direction; no live run has actually made Kokoro fail.
- **Single voice, single machine.** All timings are this WSL2 host with a warm ONNX cache. The
  120 s `TTS_STOP_FRAME_TIMEOUT_SECS` is sized to be far above the 19.1 s worst case observed
  here, not derived from a distribution.
- **Overlapping utterances.** The buffer keys on nothing — `TTSStartedFrame` clears the deque
  unconditionally — so two concurrent TTS contexts would drop the first's audio. pipecat
  serializes contexts today, so this is latent, not live. Flagged in review as LOW; not fixed.
