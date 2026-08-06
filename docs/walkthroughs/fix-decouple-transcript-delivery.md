# Walkthrough — `fix/decouple-transcript-delivery`

*Status: **complete** (Phases 0-3; Phase 4 deferred by design). 2026-08-06. Branched from `56807fd`.*

Removes the coupling between the **app bot's** transcript reaching `listen()` and the app bot's
turn aggregator closing a turn on a timer. Design and verified open questions:
[`docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md`](../specs/2026-08-06-decouple-transcripts-from-turn-closure.md).
Decision record: `BUILDLOG.md` D24.

**Who is who** (pipecat names parties by direction, not identity): `user_aggregator` belongs to the
**app bot** (audio in), `assistant_aggregator` belongs to the **tester** (what we speak as). Labelled
pipeline: [`docs/architecture/pipeline-parties.html`](../architecture/pipeline-parties.html) — open
in a browser.

## Status

| Phase | What it changes | Commits | Evidence | Done |
|---|---|---|---|---|
| **0** | Spec + labelled-pipeline diagram committed; D24 appended; walkthrough opened | `7da3989` | — | ✅ |
| **1** | App-bot transcript emitted from the pipeline observer on `TranscriptionFrame` arrival, not on turn closure | `7b73298` | [t-1-transcript-on-frame-arrival.md](../artefacts/fix-decouple-transcript-delivery/t-1-transcript-on-frame-arrival.md) | ✅ |
| **2** | Empty-transcript signal re-homed to the STT worker (`on_empty_segment` callback) | `b836f4f` | [t-2-empty-segment-signal.md](../artefacts/fix-decouple-transcript-delivery/t-2-empty-segment-signal.md) | ✅ |
| **3** | `TURN_STOP_TIMEOUT_SECS` deleted; `session_started` note and `CLAUDE.md` refreshed | `1394e4b` | [t-3-retire-the-constant.md](../artefacts/fix-decouple-transcript-delivery/t-3-retire-the-constant.md) | ✅ |

**Phase 4 (removing the vestigial aggregator + smart-turn analyzer) is deferred** — it is a
structural change whose blast radius includes the tester side, and it gets its own design pass
(spec § "Phase 4").

**Live evidence.** `localhost:3000` was not running, so the live verification came from a probe
that plays the part of the browser shim — real pipecat child, real Silero, real faster-whisper, raw
PCM into `:9091` — rather than from a browser driving the app. It caught the branch's whole point:
a **19.68 s** Whisper decode delivered its transcript with the 240 s constant deleted and pipecat's
5 s default in force, stamped to its VAD start within 0.000 s. Scenario 3 (silent segment) was
attempted live and **not reproduced** — Whisper transcribes everything Silero accepts. Numbers,
probe scripts and what remains unverified: the Phase 3 artefact.

## Try it

```bash
uv run pytest -q                     # whole suite
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run pyright src/
```

---

## Phase 1 — the transcript is emitted where it is produced

*Commit `7b73298`. Criteria: spec § "Phase 1".*

- `_PipelineEventObserver._WATCHED` gains `TranscriptionFrame`, and `_on_pipeline_frame` emits
  `app_bot_transcript` when one is pushed (`src/voicebox/agent.py:185-190, 417-418`).
- The `on_user_turn_stopped` handler is gone (`src/voicebox/agent.py:516-518`, now a comment
  saying why). Nothing consumes the app bot's turn closure any more.
- `_emit_app_bot_transcript` lost its `aggregator_turn_started_at` parameter: voicebox's own VAD
  log is the only source of turn start (`src/voicebox/agent.py:362-388`).

**Why watching that hop works** — the aggregator *consumes* the final `TranscriptionFrame` and
never pushes it on, but observers see **pushes**, and `stt`→`user_aggregator` is a downstream push.
`tests/test_transcript_delivery.py` asserts both halves against a real `Pipeline` containing the
real aggregator, so a pipecat upgrade that changed either half fails loudly.

**Not covered:** the slow-decode scenario is 🔴 live-only; the empty-segment signal has no
production path between this commit and Phase 2's, which means a silent segment leaves its VAD
start unclaimed and the deque drifts by one. Detail in the evidence artefact.

---

## Phase 2 — the empty-segment signal comes from the component that knows

*Commit `b836f4f`. Criteria: spec § "Phase 2".*

- Whisper yields **no frame** for a segment it recovers no text from
  (`pipecat/services/whisper/stt.py:377`, `if text:`), so after Phase 1 the Task F "we tried and
  got nothing" event had no carrier — and a silent segment stopped claiming its VAD start, drifting
  the deque every later transcript claims from.
- `NonBlockingSegmentedSTT` gains an optional `on_empty_segment` callback
  (`src/voicebox/processors/nonblocking_whisper_stt.py:116-123`). The worker counts transcripts
  through a pass-through wrapper and fires the callback when the count is zero
  (`:196-215`) — `process_generator` stays the only thing that pushes frames, so pipecat's
  `ErrorFrame` dispatch is not duplicated here.
- `agent.py` sets it to `_on_empty_segment` (`src/voicebox/agent.py:396-408, 481`), which emits the
  empty-flagged transcript event and claims the silent segment's VAD start.

**Not covered:** the one line wiring the two sides together in `start()` has no unit test (reaching
it loads Whisper, Kokoro and Silero); it is on the Phase 3 dogfood checklist. Detail in the
evidence artefact.

---

## Phase 3 — the constant is gone

*Commit `1394e4b`. Criteria: spec § "Phase 3".*

- `TURN_STOP_TIMEOUT_SECS` and the `user_turn_stop_timeout` override are deleted
  (`src/voicebox/agent.py:962-966`); pipecat's 5 s default now applies to a turn nothing consumes.
- `test_turn_stop_timeout_outlives_batch_stt` → `test_no_voicebox_constant_is_sized_against_decode_speed`,
  which asserts the constant is absent **and** the default is in force
  (`tests/test_agent_surface.py:297-308`).
- `session_started`'s note and `CLAUDE.md`'s "Non-obvious facts" (`CLAUDE.md:79-93`) updated.

**After this, no value in voicebox is compared against Whisper's decode speed.** A slow machine
reports its lag through `listen()`'s `transcription_lag_secs` instead of encoding it in a constant
that was only ever measured on one Linux CPU box.

**Not covered:** no dogfood session against the real voice app (it was not running); scenario 3 not
reproduced live; only the first of two utterances was carried to a transcript in the probe. Detail
in the evidence artefact.

## Deferred

**Phase 4 — removing the vestigial turn machinery** (`LLMContextAggregatorPair`, the user-turn stop
strategies, `LocalSmartTurnAnalyzerV3`). They now have no consumer on the app-bot side, but tester
frames still route *through* the app-bot aggregator on their way to the TTS, so removing it changes
the tester path. It gets its own design pass (spec § "Phase 4", `BUILDLOG.md` D24).
