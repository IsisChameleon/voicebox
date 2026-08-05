# Task F — the transcript-loss holes are closed

*Evidence for the success criteria in
[`docs/specs/2026-07-31-fix-plan-execution.md`](../../specs/2026-07-31-fix-plan-execution.md)
§ "Task F — Close the transcript-loss holes".*

Captured 2026-08-03. Commit `5d67578`. Decisions: `BUILDLOG.md` D13 (drain budget), D14 (lag
field unchanged).

## Criteria → evidence

| # | Criterion | What landed | Evidence |
|---|---|---|---|
| 1 | `stop()` awaits the STT drain — bounded (~15 s) — before `_dump_artifacts`; expiry logs and proceeds | **Adapted (D13)**: budget scales with backlog (15 s base + 1 s per queued audio second, 180 s cap) because round 3 measured a 140 s backlog; a flat 15 s would still lose data. Drain + a ≤2 s event-log settle run before the dump; expiry logs a warning and proceeds | `test_pending_transcript_reaches_artifacts`, `test_stop_bounded_when_drain_stalls`, `test_drain_budget_scales_with_backlog` |
| 2 | The `if message.content:` gate no longer swallows empty results; event carries `transcription_empty: true` | gate removed; `TranscriptEvent.transcription_empty` added; empty events also claim their VAD start so the D10 deque stays aligned | `test_empty_transcription_still_emits_event`, `test_empty_transcript_still_claims_a_vad_start` |
| 3 | `metrics.py` tolerates empty-text transcripts without counting them as real | already Task B's shape — `_spoken_transcripts` (`metrics.py`) filters on `e.get("text")`; nothing duplicated | existing `test_metrics.py` empty-transcript tests (B1 fixture's fourth utterance) |
| 4 | `stop()` ordering otherwise untouched: `SESSION_STOPPED` still first | unchanged; drained transcripts land *after* `session_stopped` in the log but inside the artifacts — asserted explicitly | ordering assertion inside `test_pending_transcript_reaches_artifacts` |

## Test run

```
$ uv run pytest -q tests/test_stop_drains_stt.py
.......                                                                  [100%]
7 passed in 3.26s

$ uv run pytest -q
59 passed, 4 warnings in 50.22s

$ uv run ruff check src/ tests/
All checks passed!
$ uv run ruff format --check src/ tests/
20 files already formatted
```

`uv run pyright src/` at the 2-error baseline (unchanged).

## Why the drain can matter this much

Round 3 (r3 artefact): a `stop()` issued 11.3 s after the bot's last speech lost four turns
(~100 s of speech) because the backlog was 60–140 s. With this commit the same session would
wait up to its computed budget (~155 s for that backlog) and write the full log; a genuinely
wedged Whisper still cannot hang teardown past the cap (F3).

## Not covered

* **Live re-verification.** Everything above is unit-level; the next live session should
  repeat round 3's prompt-stop and observe zero lost turns (and an honest warning if the cap
  ever truncates).
* **A *failed* segment still surfaces only as a log line** (worker catches, drops, moves on).
  `transcription_empty` marks empty *results*, not errors — same gap Task E's artefact noted.
* **Merged utterances** (aggregator folding two intervals into one event) remain: F emits one
  event per `UserTurnStoppedMessage`, and a merge upstream of that is invisible here (r3
  artefact; the full fix is the out-of-scope aggregator redesign).
* **Drained transcripts land after `session_stopped` in the event log** — a `listen()` loop
  that exits on `session_stopped` will not see them; the artifacts do. Accepted consequence
  of criterion 4's mandated ordering.
