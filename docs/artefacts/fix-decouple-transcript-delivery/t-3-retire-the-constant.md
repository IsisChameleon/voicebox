# Phase 3 — retiring `TURN_STOP_TIMEOUT_SECS`, and the live evidence

*2026-08-06. Commit `1394e4b`. Decision: `BUILDLOG.md` D24.*

Evidences § "Phase 3 — Retire the constant" in
[`docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md`](../../specs/2026-08-06-decouple-transcripts-from-turn-closure.md),
and scenarios 1, 2 and 4 from § "Scenarios". Scenario 3 is **not** evidenced live — see
"Not covered".

## The static half

| Spec row | Change | Verification |
|---|---|---|
| 3.1 | `TURN_STOP_TIMEOUT_SECS` and the `user_turn_stop_timeout` override deleted | `grep -rn TURN_STOP_TIMEOUT_SECS src/ tests/ scripts/` returns only the assertion that it is gone |
| 3.2 | `session_started` note updated — the transcript still trails speech, but now because of batch STT alone, and `transcription_lag_secs` is named as where to read the machine's lag | appears verbatim in every probe event log below |
| 3.3 | `CLAUDE.md` "Non-obvious facts" rewritten for D24 | `CLAUDE.md:79-93` |

`test_turn_stop_timeout_outlives_batch_stt` is replaced by
`test_no_voicebox_constant_is_sized_against_decode_speed`, which asserts both that the module no
longer defines the constant and that pipecat's own 5.0 s default
(`llm_response_universal.py:158`) is what the aggregator now runs with.

```
$ uv run ruff format src/ tests/ && uv run ruff check src/ tests/
26 files left unchanged
All checks passed!

$ uv run pyright src/
  src/voicebox/agent.py:548:42 - error: "start_recording" is not a known attribute of "None" (reportOptionalMemberAccess)
  src/voicebox/browser_session.py:39:23 - error: Variable not allowed in type expression (reportInvalidTypeForm)
2 errors, 0 warnings, 0 informations

$ uv run pytest -q
87 passed in 59.69s
```

Same two pre-existing pyright errors as Phases 1 and 2; the branch adds none.

## The live half

The app under test (`localhost:3000`) was not running, so instead of a dogfood session the probe
below plays the part of the browser shim directly: it runs the **real** pipecat child — real Silero
VAD, real faster-whisper, real `start()` — and streams 16 kHz PCM into `:9091` over a raw
WebSocket, which is byte-for-byte what `shim.js` sends. Input is a real recording of the app bot
from dogfood round 6 (`temp/verify-round-6/ember_voice.wav`), decimated 48→16 kHz.

Script: [`probe_live_transcript_delivery.py`](probe_live_transcript_delivery.py).

```
$ uv run python docs/artefacts/fix-decouple-transcript-delivery/probe_live_transcript_delivery.py

voicebox.agent:start:452 - Starting Pipecat MCP Agent pipeline...
voicebox.agent:start:554 - Pipecat MCP Agent started!
pipecat.transports.websocket.server - Starting websocket server on localhost:9091
  event session_started            t=+   8.62s
  event client_connected           t=+   7.03s
  event app_bot_speech_started     t=+   8.43s
  event app_bot_speech_stopped     t=+  18.47s   (transcription_lag_secs=0.052)
pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy - VAD stop_secs (1.0s) >= STT
  p99 latency (1.0s). STT wait timeout collapsed to 0s, which may cause delayed turn detection
  specified by the user_turn_stop_timeout parameter in the LLMUserAggregatorParams.
  event app_bot_speech_started     t=+  20.23s   (transcription_lag_secs=1.76)
  event app_bot_speech_stopped     t=+  26.74s   (transcription_lag_secs=8.32)
  event app_bot_transcript         t=+  28.12s   (transcription_lag_secs=1.374)

=== RESULT ===
transcript text          : " Hello there. Welcome. I'm so glad you're here. We're about to go on
                            an amazing adventure together. Let me check what book we have waiting
                            for you today. "
transcription_empty      : False
VAD start t              : +8.43s
turn_started_at (claimed): +8.43s
transcript arrived at t  : +28.12s
decode lag (arrival-VAD) : 19.68s
claimed vs VAD start     : 0.000s  (must be ~0)
```

Three things in that run are the point of the whole branch.

**1. The decode took 19.68 s and the transcript still arrived — with no 240 s constant protecting
it.** This build has `TURN_STOP_TIMEOUT_SECS` deleted, so the aggregator is running on pipecat's
5.0 s default. Under the old design a 19.68 s decode against a 5 s watchdog is precisely the case
that closed the turn empty and re-emitted the text later as a mistimed orphan — which is why the
240 s override existed. Delivery no longer passes through that timer at all, so the number that had
to be ≥ worst-case decode time is simply gone. Scenarios 1 and 4.

**2. pipecat itself warned that turn detection would be delayed — and it no longer matters.** The
`TurnAnalyzerUserTurnStopStrategy` warning in the middle of the run says the STT wait timeout
collapsed to 0 s and turn detection may be delayed "specified by the `user_turn_stop_timeout`
parameter". That warning is about the machinery voicebox has stopped depending on; the transcript
arrived anyway, on its own utterance's timestamp.

**3. `turn_started_at` matched the VAD start to 0.000 s.** The transcript is stamped with the start
of the utterance it belongs to, not with its own arrival 19.68 s later — D10's rule carried through
to delivery. Scenario 2.

## Scenario 3 (silent segment): attempted live, not reproduced

The empty-segment path needs audio Silero calls speech but Whisper finds no words in. Three
candidates were streamed through one real session
([`probe_live_empty_segment.py`](probe_live_empty_segment.py)):

```
=== RESULT ===
candidate                         VAD=speech  transcripts  text / empty flag
speech-band noise                      False            0  —
reversed real speech                    True            1  empty=False text=" The evening is he a maninawagg's lover.  Hurry, hurried out was me. "
voiced buzz (120 Hz sawtooth)          False            0  —

empty-flagged transcripts observed: 0
```

Silero rejects anything that is not speech-like, and Whisper produces text for everything Silero
accepts — it hallucinated words out of reversed speech rather than returning nothing. **The live
empty-segment event was therefore not observed, and this artefact does not claim it.** The finding
is itself worth recording: the empty case is rare in practice, which is consistent with it having
been a readability signal rather than a metric input (spec Q1 — `metrics.py:221` already filters it
out).

Coverage for that path stays where Phase 2 put it: `tests/test_nonblocking_stt.py::test_empty_segment_signals_once_and_only_when_silent`,
whose Whisper stand-in returns without yielding — faithful to the real `if text:` guard at
`pipecat/services/whisper/stt.py:377`.

What the probes **do** close from the Phase 2 artefact's "not covered": the wiring line
`stt.on_empty_segment = self._on_empty_segment` (`src/voicebox/agent.py:481`) runs inside a real
`start()` — five probe sessions started cleanly against the real `_NonBlockingWhisperSTTService`,
so the attribute assignment is valid on the production class, not just on the test double. Only the
callback *firing* is unproven live.

## Not covered

* **A full dogfood session against the real voice app.** `localhost:3000` was not running
  (`curl` → 000; the readme repo's Supabase containers are up but its app is not), so the live
  verification came from the shim-shaped probe rather than a browser driving EmberTales. The probe
  covers the audio path from the tap onward — it does **not** cover `shim.js`, the browser child,
  CDP attach, or `speak()` playing into a real page. Those are unchanged by this branch, but they
  are also not re-verified by it.
* **Scenario 3 live**, as above.
* **The second utterance's transcript.** The probe stops at the first `app_bot_transcript`, so the
  segment that VAD-started at +20.23 s was still decoding when `stop()` was called. Nothing about
  the ordering of two concurrent transcripts is evidenced here; the deque-claiming order is covered
  by `tests/test_stop_drains_stt.py::test_empty_transcript_still_claims_a_vad_start`.
* **Non-Linux backends.** Everything above ran on faster-whisper CPU int8. The MLX path
  (`_NonBlockingWhisperSTTServiceMLX`) is untouched by this branch and untested here — though the
  point of the change is that it no longer needs a machine-specific constant either.
