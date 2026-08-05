# Task D — the VAD moves upstream of the STT

*Evidence for the success criteria in
[`docs/specs/2026-07-31-fix-plan-execution.md`](../../specs/2026-07-31-fix-plan-execution.md)
§ "Task D — Move the VAD upstream of the STT".*

Captured 2026-08-01. Commit `ad93dd4`.

## Criteria → evidence

| # | Criterion (as specced) | What landed | Evidence |
|---|---|---|---|
| 1 | `WebsocketServerParams` carries `vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS))` | **Adapted** — that field does not exist in pipecat 1.3.0. A `VADProcessor` stage sits between `transport.input()` and the STT instead. See BUILDLOG D3 and the API check below | `agent.py:_create_vad_processor`, `agent.py:_build_stages` |
| 2 | `vad_analyzer=` removed from `LLMUserAggregatorParams` | done | `test_aggregator_does_no_vad_of_its_own` |
| 3 | `VAD_STOP_SECS` stays one constant, read by the VAD and the `session_started` header | done | `test_vad_analyzer_keeps_the_one_second_stop` asserts `params.stop_secs == VAD_STOP_SECS == 1.0` |
| 4 | `_PipelineEventObserver` unchanged | unchanged — it matches frame types, not producers, and `VADProcessor.broadcast_frame` still pushes one downstream frame per event | `git show ad93dd4 -- src/voicebox/agent.py` touches no observer line |
| 5 | The "why 1.0 s, not 0.2 s" rationale moves with the parameter | moved verbatim into `_create_vad_processor`'s docstring | `agent.py` |

## The API the spec assumed is gone

The spec was written against a pipecat whose `TransportParams` carried the VAD. Checked
against the installed version rather than assumed:

```
$ uv run python -c "
from pipecat.transports.websocket.server import WebsocketServerParams
print('vad_analyzer' in WebsocketServerParams.model_fields)"
ᓚᘏᗢ Pipecat 1.3.0 (Python 3.12.3) ᓚᘏᗢ
False
```

In 1.3.0 the VAD is a pipeline stage: `pipecat/processors/audio/vad_processor.py:27`
(`VADProcessor`), which wraps a `VADController` and pushes
`VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame` via `broadcast_frame` —
downstream *and* upstream, the same broadcast the aggregator used to do. So the change is a
change of position in the pipeline, which is what the task is about.

## D1 — the flood survives a stalled STT

`tests/test_vad_placement.py` runs the same 10 s flood through both topologies with a
deterministic stub VAD analyzer (non-zero bytes = speech), so nothing depends on ONNX
inference over synthetic audio:

```
$ uv run python - <<'PY'   # throwaway; drives the test module's own helpers and prints the numbers
# sys.path.insert(0, "tests"); from test_vad_placement import *
# up   = _StallingSTT(); a = await _run_flood_through(up,   [_stub_vad(), up, _Sink()])
# down = _StallingSTT(); b = await _run_flood_through(down, [down, _stub_vad(), _Sink()])
PY
FLOOD fed: 10.0s
  VAD UPSTREAM   -> run_stt saw 10.20s  (lost -0.20s, -2%)
  VAD DOWNSTREAM -> run_stt saw 1.50s   (lost +8.50s, 85%)
  segment lists: upstream=[2.3, 10.2] downstream=[1.0, 1.5]
```

The mechanism, confirmed in pipecat's source:

* `VADUserStartedSpeakingFrame` is a `SystemFrame` (`pipecat/frames/frames.py:1035`), so with
  the VAD upstream it reaches the STT ahead of the audio it describes.
* `SegmentedSTTService.process_audio_frame` trims the buffer to the last second on every frame
  that arrives while `_user_speaking` is `False` (`pipecat/services/stt_service.py:805-807`).
* `_user_speaking` is set only by a `VADUserStartedSpeakingFrame` the STT *receives*
  (`stt_service.py:762-766`) — hence the placement is the whole story.

Note the downstream column also loses half of *segment 1* (1.0 s of 2.0 s) even with no stall
at all: the VAD's start frame lands after that audio has already been trimmed. The stall makes
the loss dramatic, it does not create it.

The two tests are a matched pair — a positive (`>= 9.5 s`) and a negative control
(`< 3.0 s`) — so the suite fails loudly if someone moves the stage back.

## Test run

```
$ uv run pytest -q tests/test_vad_placement.py
.....                                                                    [100%]
5 passed, 3 warnings in 15.52s

$ uv run pytest -q
34 passed, 3 warnings in 33.94s

$ uv run ruff check src/ tests/
All checks passed!

$ uv run ruff format --check src/ tests/
16 files already formatted
```

`uv run pyright src/` reports the same 2 errors as the pre-task baseline
(`agent.py` `start_recording` on an `Optional`, `browser_session.py:35`) — verified by
stashing the change and re-running. No new errors.

## Not covered

* **D3 🔴 (live-only)** — that `app_bot_speech_stopped - app_bot_speech_started` matches the
  app's own logged speech duration to within `vad_stop_secs`, and that transcripts now start at
  sentence boundaries. Needs a real voice app on `localhost:3000`.
* **Every number in `metrics.json` moves.** VAD timestamps are now acoustically true rather
  than post-STT, so `app_bot_speech_*` shifts earlier. That is the point of the task, but it
  means metrics from before this commit are not comparable with metrics after it.
* **Silero itself is not exercised by these tests** — the stub analyzer is. What is proven is
  placement, not detection quality. `test_vad_analyzer_keeps_the_one_second_stop` does
  construct the real `SileroVADAnalyzer` and check its params.
* **The turn analyzer is untouched.** If Task C's live run attributes the ~24 s lag to
  smart-turn inference, that is a separate change (execution spec § "Out of scope").
