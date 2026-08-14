# Generated utterance audio buffer — 4+1 delta

Issues: [#19](https://github.com/IsisChameleon/voicebox/issues/19) (stock Kokoro),
[#24](https://github.com/IsisChameleon/voicebox/issues/24) (real-pipeline tests, first port).
Decisions: [BUILDLOG](../../../BUILDLOG.md) D30–D33.

This delta replaces the fork-by-copy Kokoro Text-to-Speech (TTS) service with stock pipecat Kokoro
and moves gap-free generated-audio playout to a backend-independent pipeline processor. A second
round (2026-08-14) makes the tests able to observe that processor at all, and removes the dead
`localhost:3000` default from the tool surface.

## Scenarios

### S1: A generated utterance contains slow synthesis chunks

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | `speak()` queues one LLM response triplet | logical | `src/voicebox/agent.py:_queue_speak_frames` |
| 2 | TOKEN aggregation gives stock Kokoro one synthesis context; `stop_frame_timeout_s` raised to 120 s so the base class does not close the context first (D31) | logical | `src/voicebox/agent.py:984-989` |
| 3 | Generated audio frames accumulate without reaching the transport | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:64-65` |
| 4 | TTS stop releases saved frames in order, then the stop marker | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:66-70` |
| 5 | The WebSocket transport and shim present one contiguous mic span to the target | physical | `src/voicebox/agent.py:1034-1041`, `src/voicebox/shim.js:72-92` |

Given synthesis chunks have delays, when an utterance completes, then no generated audio reaches
the transport before completion and all of it is released in original order before TTS stop.
Protected by `tests/test_generated_utterance_audio_buffer.py::test_generated_audio_is_withheld_until_its_utterance_closes`
and, end to end through the real service,
`tests/test_generated_utterance_in_pipeline.py::test_one_speak_produces_exactly_one_tts_bracket`.

### S2: Playout is interrupted before the audio reaches the transport

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | Some generated audio is waiting in the buffer | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:64-65` |
| 2 | `InterruptionFrame`/`CancelFrame`/`EndFrame` clears the unplayed frames and continues downstream | process | `src/voicebox/processors/generated_utterance_audio_buffer.py:71-73` |
| 3 | `_Playout` resolves the interruption without waiting for a later audio stop | logical | `src/voicebox/agent.py:_Playout.on_interrupted` |

Given generated audio has not reached the transport, when playout is interrupted, then the partial
utterance is never played. Protected by
`test_interruption_discards_generated_audio_that_has_not_reached_playout`.

**Synthesis FAILURE is the sibling case and it does NOT hold** — see Gaps. The previous version of
this delta claimed it did.

### S3: A developer verifies the audio path with nothing else running

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | Script serves one empty page on an ephemeral localhost port | physical | `scripts/blank_page_server.py:33-46` |
| 2 | pipecat child spawned in browser-shim mode; ports preflighted | process | `scripts/smoke_browser_shim.py:49-53` |
| 3 | Chromium launched with the shim injected, navigated to that page (a secure origin, which the shim's hooks require) | physical | `scripts/smoke_browser_shim.py:60-66`, `src/voicebox/shim.js` hook gates |
| 4 | `speak()` over IPC; Kokoro audio crosses the WebSocket into the page | process | `scripts/smoke_browser_shim.py:110-113` |
| 5 | `window.__voiceShim.inboundChunks > 0` decides the run; non-zero exit otherwise | development | `scripts/smoke_browser_shim.py:120-126`, `:139-142` |

Given no voice app and no network, when the smoke script runs, then Kokoro audio reaches the page
or the run fails. Live-only: no pytest coverage (Gaps row 2). Measured 2026-08-14: 40 chunks,
~25 s, exit 0.

### S4: The LLM starts a session and must name the app under test

| # | Hop | View | Evidence |
|---|---|---|---|
| 1 | `start_browser_session(url)` — `url` has no default | development | `src/voicebox/server.py:129-136` |
| 2 | Docstring states the requirement and the secure-origin constraint | development | `src/voicebox/server.py:158-162` |

Given voicebox exists to test the caller's app, when a session is started, then the app is named
explicitly. Protected by `tests/test_server_tool_surface.py::test_the_app_under_test_must_be_named`
(mutation-checked: restoring the old default turns it red).

## View impact

| View | Changed? | What |
|---|---|---|
| Logical | YES | Gap-free playout is `GeneratedUtteranceAudioBuffer` policy; Kokoro is stock, with the base class owning the TTS start/stop bracket. |
| Process | YES | A stage after TTS withholds generated frames until TTS stop; interruption clears them. Unchanged in round 2. |
| Development | YES | Deletes the vendored Kokoro module; adds the processor and warm-up helper; **round 2**: processor tests move to real pipelines, the smoke scripts stop depending on an external app, and a tool signature becomes required. |
| Physical | YES (round 2, dev-only) | The smoke path now binds one extra ephemeral localhost port for its own page. No change to the 9090/9091/9222 session ports. |

**The process view did not change in round 2**, which is the point: the round-2 work changes how we
*observe* the process view, not what it does.

## Tests derived from the scenarios

| Scenario | Contract | Test | State |
|---|---|---|---|
| S1 | Withhold, preserve order, flush before stop | `test_generated_audio_is_withheld_until_its_utterance_closes` | ✅ mutation-checked (A) |
| S1 | One `speak()` = exactly one TTS bracket | `test_one_speak_produces_exactly_one_tts_bracket` | ✅ |
| S1 | Production uses stock Kokoro, one synthesis per speak | `test_voicebox_uses_stock_kokoro_once_per_speak` | ✅ |
| S1 | Buffer occupies the immediate post-TTS stage | `test_vad_stage_precedes_the_stt` | ✅ |
| S1 | Warm-up runs once the pipeline sets a sample rate, and its audio never enters the pipeline | `test_warm_up_synthesizes_once_the_pipeline_hands_over_a_sample_rate` | ✅ mutation-checked (D) |
| S2 | Never resurrect interrupted buffered speech | `test_interruption_discards_generated_audio_that_has_not_reached_playout` | ✅ mutation-checked (C) |
| S2 | Do not disturb non-generated or upstream traffic | `test_upstream_frames_pass_through_unchanged` | ✅ |
| S2 | An error from BELOW discards the partial utterance | `test_an_error_from_below_discards_the_partial_utterance` | ✅ mutation-checked (B) |
| S2 | A synthesis FAILURE discards the partial utterance | `test_synthesis_failure_routes_upstream_and_drops_partial_audio` | ❌ `strict=True` xfail — issue #25 |
| S3 | Audio reaches the page with no app running | `scripts/smoke_browser_shim.py` | ⚠ live-only |
| S4 | `url` stays required | `test_the_app_under_test_must_be_named` | ✅ mutation-checked |

Written this round after the trace showed them missing: S4 had **no** test (the previous default
outlived its app by three PRs unnoticed), and S1's warm-up contract was only tested against a fake
service that set `sample_rate` itself — the one thing the real defect turned on.

## Fold into the living core

Folded into `docs/architecture/4plus1.md` in this PR:

- **I4 corrected.** It asserted "partial audio is discarded on failure/interruption". The failure
  half is not implemented (D32). Split so the invariant states only what holds, with the failure
  case moved to Gaps and issue #25.
- S3 hop 5 citations refreshed to the current line ranges.
- Gaps row 2 (no pytest coverage of the audio path) updated: the compensating control now needs no
  external app and can actually fail.
- New Gaps row for the unimplemented failure contract.

Delta scenarios S1–S2 stay local (they refine core S3). S3 and S4 stay local: S3 is a developer
workflow, not a system scenario, and S4 is one hop of core S1.
