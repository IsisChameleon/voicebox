# Phase 1 — the app bot's transcript is emitted where it is produced

*2026-08-06. Commit `7b73298`. Decision: `BUILDLOG.md` D24.*

Evidences the Phase 1 table and its **Verify** paragraph in
[`docs/specs/2026-08-06-decouple-transcripts-from-turn-closure.md`](../../specs/2026-08-06-decouple-transcripts-from-turn-closure.md)
§ "Phase 1 — Emit the app bot's transcript where it is produced", and scenario 2
("fast decode, unchanged behaviour") from § "Scenarios". Scenario 1 (slow decode) is 🔴 live-only —
see "Not covered".

## The claim under test

Q4 of the spec: the app bot's aggregator **consumes** the final `TranscriptionFrame` and never
pushes it downstream, so nothing after the aggregator can see it — but the observer watches
processor→processor *pushes*, and the `stt`→`user_aggregator` hop is a downstream push. The spec
answered this by reading pipecat
(`.venv/.../pipecat/processors/aggregators/llm_response_universal.py:696-700`). This artefact
answers it by running it.

## Both halves of Q4, against a real pipeline

`tests/test_transcript_delivery.py` builds a real `Pipeline` holding the **real**
`LLMUserAggregator` (from `agent._create_context_aggregators`), with a relay on each side, and the
real `_PipelineEventObserver` attached to the `PipelineWorker`:

```python
worker = PipelineWorker(
    Pipeline([stt_stand_in, user_aggregator, downstream_of_aggregator]),
    cancel_on_idle_timeout=False,
    enable_rtvi=False,
    observers=[agent_module._PipelineEventObserver(agent)],
)
...
await worker.queue_frame(TranscriptionFrame("the bot said this", "", "iso"))
```

and asserts, in one test:

```python
transcripts = [e for e in agent._events if e.type == EventType.APP_BOT_TRANSCRIPT]
assert len(transcripts) == 1
assert transcripts[0].text == "the bot said this"

# the frame never gets past the aggregator — watching any later hop sees nothing
assert not [f for f in downstream_of_aggregator.seen if isinstance(f, TranscriptionFrame)]
assert [f for f in stt_stand_in.seen if isinstance(f, TranscriptionFrame)]
```

The second and third assertions are the load-bearing ones: they show the frame **does** reach the
hop we watch and **does not** survive the aggregator. If a future pipecat made the aggregator
forward the frame, or moved the consumption earlier, this test says so.

## Test run

```
$ uv run pytest tests/test_transcript_delivery.py \
    tests/test_agent_surface.py::test_transcription_frame_emits_transcript_on_arrival \
    tests/test_agent_surface.py::test_transcription_frame_emitted_once_across_hops \
    tests/test_agent_surface.py::test_upstream_pushes_are_ignored \
    tests/test_agent_surface.py::test_transcript_turn_started_at_uses_observed_vad_start -v

tests/test_transcript_delivery.py::test_transcript_is_delivered_at_the_hop_into_the_aggregator PASSED [ 20%]
tests/test_agent_surface.py::test_transcription_frame_emits_transcript_on_arrival PASSED [ 40%]
tests/test_agent_surface.py::test_transcription_frame_emitted_once_across_hops PASSED [ 60%]
tests/test_agent_surface.py::test_upstream_pushes_are_ignored PASSED     [ 80%]
tests/test_agent_surface.py::test_transcript_turn_started_at_uses_observed_vad_start PASSED [100%]
============================== 5 passed in 2.42s ===============================
```

Whole suite, lint, format, types:

```
$ uv run ruff format src/ tests/ && uv run ruff check src/ tests/
26 files left unchanged
All checks passed!

$ uv run pyright src/
  src/voicebox/agent.py:542:42 - error: "start_recording" is not a known attribute of "None" (reportOptionalMemberAccess)
  src/voicebox/browser_session.py:39:23 - error: Variable not allowed in type expression (reportInvalidTypeForm)
2 errors, 0 warnings, 0 informations

$ uv run pytest -q
85 passed, 6 warnings in 56.25s
```

Both pyright errors are pre-existing — the identical two lines and count are produced by
`git stash && uv run pyright src/` on the parent commit. This phase adds none.

## Mutation check — the tests fail without the change

Removing `TranscriptionFrame` from `_PipelineEventObserver._WATCHED` and nothing else:

```
$ uv run pytest tests/test_transcript_delivery.py tests/test_agent_surface.py -q
FAILED tests/test_transcript_delivery.py::test_transcript_is_delivered_at_the_hop_into_the_aggregator
FAILED tests/test_agent_surface.py::test_transcription_frame_emits_transcript_on_arrival
FAILED tests/test_agent_surface.py::test_transcription_frame_emitted_once_across_hops
3 failed, 13 passed, 3 warnings in 3.70s
```

The file was restored from a copy afterwards and `git diff --stat` re-confirmed the 38/31 line
change, so nothing from the mutation reached the commit.

## What changed in `src/`

`src/voicebox/agent.py`, four edits matching the spec's Phase 1 table:

| Spec row | Change |
|---|---|
| 1.1 | `TranscriptionFrame` added to `_PipelineEventObserver._WATCHED`, with the Q4 reasoning as a comment at the point of use |
| 1.2 | `_on_pipeline_frame` gains `elif isinstance(frame, TranscriptionFrame): await self._emit_app_bot_transcript(frame.text)` |
| 1.3 | `_emit_app_bot_transcript(text, aggregator_turn_started_at)` → `_emit_app_bot_transcript(text)`; the VAD log is the only source of turn start, falling back to `time.time()` only when no start was ever observed |
| 1.4 | the `on_user_turn_stopped` handler is deleted, replaced by a comment saying why there is none |

Orphans removed by this change alone: the `UserTurnStoppedMessage` import and the `log_duration`
import in `agent.py` (its only remaining use was the deleted handler). `log_duration` itself stays
in `timing.py` and is still used elsewhere.

## Not covered

* **🔴 Scenario 1 — slow decode, correct report.** Needs a live session where a Whisper decode
  outlasts pipecat's 5 s turn-stop default, against a real voice app on `localhost:3000`. It is the
  point of the whole change and `pytest` cannot show it. Checked in the Phase 3 dogfood session.
* **The empty-transcript path is not yet re-homed.** After this phase, a segment Whisper recovers
  nothing from produces *no* event at all — `run_stt` yields nothing, so there is no frame to
  observe. `test_empty_transcription_still_emits_event` still passes because it calls
  `_emit_app_bot_transcript("")` directly, so it now tests the helper rather than a production
  path. Phase 2 restores the production path and re-points that test.
  Consequence in the meantime: a silent segment leaves its VAD start unclaimed, so the
  `_unclaimed_bot_speech_starts` deque drifts by one and later transcripts claim a neighbour's
  start. This is a real regression that exists only between this commit and Phase 2's.
* **`TURN_STOP_TIMEOUT_SECS` is untouched**, and `test_turn_stop_timeout_outlives_batch_stt` still
  asserts it. Phase 3 deletes both.
* **Frame-id dedup across a real multi-hop traverse** is asserted by calling `on_push_frame` twice
  with the same instance (`test_transcription_frame_emitted_once_across_hops`), not by observing a
  frame that genuinely traverses several hops — the aggregator consumes it at the first one, so no
  such traverse exists for this frame type.
