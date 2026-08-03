# Round 6 — blind live verification (multi-sentence stress + prompt stop)

*Captured 2026-08-03. Fix commit `248446c` (code), decision `BUILDLOG.md` D17.
Session artefacts: `temp/verify-round-6/` (gitignored). Final round of the post-G loop; the
tester deliberately opened with a 3-sentence, 70-word first utterance.*

## What held up

* **F1 again, harder:** `stop()` 2.6 s after the final speech; the final transcript reached
  `events.json` (72 s after `session_stopped`) along with the whole 3-transcript backlog —
  9/9 bot utterances have their text somewhere in the log, zero `transcription_empty`.
* Response latencies metronomic (4.2–5.5 s, mean 4.5); barge-in fired at trigger +1.504 s,
  produced ~1.6 s of true overlap, and Ember visibly cut off mid-sentence and answered the
  interjection. `turn_started_at` exact to the millisecond on the first three checked turns.
* App bootstrap, cursors, `session_stopped`-to-pending-listener delivery: all clean.

## Found and fixed this round (commit `248446c`, D17)

**The splits were never (only) cold-start — they're per-sentence synthesis.** pipecat's
SENTENCE aggregation gives each sentence its own `run_tts` call, so Task G's buffering covered
sentences, not utterances: the 3-sentence opener produced 3 speech pairs (2.3 s gap), a later
2-sentence speak produced a 1.5 s fragment then an **11.5 s** silent gap (synthesis starved by
Whisper's CPU load), Ember answered the fragment, and the tail talked over her reply.
`wait_for_playout` resolved at the first sentence's `TTSStoppedFrame` — claiming a 13 s
utterance finished in 1.5 s. Fix: `TextAggregationMode.TOKEN` — voicebox sends one
`LLMTextFrame` per `speak()`, TOKEN passes it through whole, one `run_tts`/one span/one
TTSStopped per utterance. Pinned by `test_multi_sentence_speak_is_one_synthesis_call`.

**The "slow barge-in arm" is closed as not-a-defect, with log proof:** command receipt →
armed event in **1 ms** (`agent-debug.log` lines 208–209). The 8.5 s the testers kept
measuring is their own turnaround before issuing the call. Docstring guidance goes in Task H:
arm early; arming is instant server-side.

## Known and documented, not fixed here

* **`stop()` exceeded the MCP client's ~60 s cap** on this 16-minute session (teardown
  70–95 s: 72 s draining three backlogged transcripts + 400 MB of WAVs). The teardown
  completed and every artifact landed; the caller just never received the paths. Now stated
  in `stop()`'s docstring (poll `record_dir` on client timeout). A real fix is progress
  notifications, out of scope here.
* **Aggregator merge → off-by-one drift**: one transcript event covered bot turns 4+5, after
  which every `turn_started_at` and every metrics turn-text was shifted one turn early,
  including a false `transcript_missing` on the final turn. Recorded as decisive evidence for
  the already-specced aggregator-redesign follow-up (per-segment transcript emission) — D17.
* **One-turn-behind delivery / 328 s worst lag** during long narrations, and `dead_air_gaps`
  booking the tester's own (now-fixed) synthesis gaps as app dead air — both consequences of
  the above two, re-measured for the record; issue #13 carries the decode-speed side.

## Quality gates

Suite 64 → 65 passing; ruff + format clean; pyright at the 2-error baseline.

## Not covered

* TOKEN-mode synthesis is pinned at the aggregator level but not yet live-verified (this was
  the last tester round of the loop); the next live session should show a multi-sentence
  first utterance as exactly one speech pair, and `wait_for_playout` spanning it fully.
* Kokoro synthesis latency for a whole long utterance in one call will exceed the
  per-sentence path's time-to-first-audio by design (G's stated trade-off); unmeasured.
* The stop-timeout docstring lands on the next server restart.
