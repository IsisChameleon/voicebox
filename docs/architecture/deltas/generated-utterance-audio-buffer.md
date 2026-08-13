# Generated utterance audio buffer — 4+1 delta

Issue: [#19](https://github.com/IsisChameleon/voicebox/issues/19). This delta replaces the
fork-by-copy Kokoro service with stock pipecat Kokoro and moves gap-free generated-audio playout
to a backend-independent pipeline processor.

## Scenarios

### S1: A generated utterance contains slow synthesis chunks

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | `speak()` queues one LLM response triplet | logical | `src/voicebox/agent.py:_queue_speak_frames` |
| 2 | TOKEN aggregation gives stock Kokoro one synthesis context | logical | `src/voicebox/agent.py:981-985` |
| 3 | Generated audio frames accumulate without reaching the transport | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:45-50` |
| 4 | TTS stop releases saved frames in order, then the stop marker | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:51-55` |
| 5 | The WebSocket transport and shim present one contiguous mic span to the target | physical | `src/voicebox/agent.py:1025-1032`, `src/voicebox/shim.js:72-92` |

Given synthesis chunks have delays, when an utterance completes, then no generated audio reaches
the transport before completion and all of it is released in original order before TTS stop.
Protected by `tests/test_generated_utterance_audio_buffer.py::test_generated_audio_is_withheld_then_flushed_before_its_stop`.

### S2: Generation fails or is interrupted before playout

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | Some generated audio is waiting in the buffer | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:45-50` |
| 2 | Error/interruption clears the unplayed frames and continues downstream | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:56-59` |
| 3 | `_Playout` resolves interruption without waiting for a later audio stop | logical | `src/voicebox/agent.py:_Playout.on_interrupted` |

Given generated audio has not reached the transport, when synthesis fails or playout is
interrupted, then the partial utterance is never played. Protected by the error and interruption
tests in `tests/test_generated_utterance_audio_buffer.py`.

## View impact

| View | Changed? | What |
|---|---|---|
| Logical | YES | Gap-free playout becomes `GeneratedUtteranceAudioBuffer` policy; Kokoro is stock. |
| Process | YES | A stage after TTS withholds generated frames until TTS stop; cancellation clears them. |
| Development | YES | Deletes the vendored Kokoro module; adds focused processor and generic warm-up helper. |
| Physical | no | Same processes, models, WebSocket, sample rates, and ports. |

## Tests derived from the scenarios

| Contract | Test |
|---|---|
| Withhold, preserve identity/order, flush before stop | `test_generated_audio_is_withheld_then_flushed_before_its_stop` |
| Do not disturb non-generated or upstream traffic | `test_non_generated_and_upstream_frames_pass_through_unchanged` |
| Never play a failed partial utterance | `test_synthesis_error_discards_a_partial_generated_utterance` |
| Never resurrect interrupted buffered speech | `test_interruption_discards_generated_audio_that_has_not_reached_playout` |
| Warm-up consumes the provider stream without pipeline output | `test_warm_up_consumes_the_service_stream_without_forwarding_audio` |
| Production uses stock Kokoro and one synthesis per speak | `test_voicebox_uses_stock_kokoro_once_per_speak` |
| Buffer occupies the immediate post-TTS stage | `test_vad_stage_precedes_the_stt` |

## Fold into the living core

Folded in this change: logical elements, generated-audio process flow, development ownership,
physical model wording, invariant I4, and scenario S3 were updated in
`docs/architecture/4plus1.md`. Delta scenarios remain local because they refine existing core S3.
