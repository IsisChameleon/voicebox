# Round 2 — blind live verification

*Captured 2026-08-03. Fix commit `2d7646d` (code), decisions `BUILDLOG.md` D10–D11.
Session artefacts: `temp/verify-round-2/` (gitignored). Fresh spec-blind tester, same
scenario as round 1, against EmberTales "The Cave of Time".*

## Round-1 fixes verified live

| Round-1 defect | Round 1 | Round 2 |
|---|---|---|
| Transcript coverage | 13/13 (one suspect duplicate) | **14/14**, no phantom utterances |
| `transcription_lag_secs` | 0.0 on every call | real values: 2.878 … 35.28 s across calls |
| `speak()` → audio starts | 4.3–12.0 s | **1.8–5.2 s** (≈ Kokoro synthesis alone) |
| `turn_started_at` | wrong on every checked turn (arrival time) | right on 7/14 — the watchdog case is gone; the overlap case remained (fixed this round, D10) |
| Barge-in timer | 1.502 s vs 1.5 configured | 1.502 s again |

App response latencies (tester stop → bot start): 4.09–8.13 s, mean 5.187 s — consistent
with `metrics.json`, which reconciled again (`utterances.app_bot: 14` = 14 turns).

## What round 2 found

1. **`turn_started_at` wrong on 7/14 — the overlap case (fixed, D10).** Exact pattern: the
   first transcript after a tester turn was right; every later chunk of the same bot
   monologue carried ≈ its own emission time (worst offset 103 s: speech started
   1785750726.26, stamped `09:53:49.257` = 1785750829.257). Cause: chunk N+1 VAD-starts
   while chunk N's transcript is still in Whisper; the open turn swallows the VAD start and
   the late transcript re-opens the turn at arrival time. Fix: the event now claims the
   earliest unclaimed VAD start from voicebox's own observer log; pipecat's stamp is only a
   fallback. Pinned by `test_transcript_turn_started_at_uses_observed_vad_start`.
2. **`transcription_lag_secs: 0.0` in the wake-up race (fixed, D11).** A `listen()` woken by
   `app_bot_speech_stopped` sampled the queue before the STT processed that same frame —
   e.g. the call returning the stop at t=1785750151.99 read 0.0 while that segment's
   transcript was 56.9 s away. The envelope now settles 50 ms before sampling when the batch
   contains a speech stop. Pinned by `test_listen_lag_sampled_after_speech_stop_settles`.
3. **`wait_for_playout` resolved after the first Kokoro burst** (`finished_at`
   1785750468.04 vs true audio end 1785750472.15 — the second burst started 1.02 s later,
   just past the 1.0 s settle window). This is exactly Task G criterion 3 (resolve on
   `BotStoppedSpeakingFrame` after `TTSStoppedFrame`, delete the settle timer). **Deferred
   to G**, now with a live reproduction.
4. **Three phantom `transcript_missing` tester turns** (`utterances.tester: 10` for 7 real
   `speak()` calls) — second bursts of split Kokoro playouts. Task G at the source.
5. **Barge-in timer semantics**: fired at +1.502 s but audio landed +7.2 s after Ember
   started (Kokoro synthesis). Candidate improvement — synthesize at arm time so firing
   plays cached audio — belongs with G's `run_tts` buffering work. Recorded, not done.
6. **Transcript-quality edges at chunk boundaries**: one mid-word split across chunks
   ("…cleared of whirling." / "know by the fierce wind…" — the word was "swirling snow"),
   one duplicated boundary word ("You…" / "You were…"), one hallucinated tail ("It's a lot
   of it."). Inherent to VAD-segmented batch Whisper; recorded, no fix planned this branch.
7. **Transcript lag 26.4–69.1 s across 14 turns.** With the loop free this is now pure
   decode+queueing (~0.5× realtime on CPU, serialized worker, backlog compounding during
   long narrations). Inherent to the pinned CPU/int8 config (execution spec: the pin stays);
   the honest lag field is the mitigation.

## Quality gates

Suite 47 → 50 passing (`test_transcript_turn_started_at_uses_observed_vad_start`,
`test_transcript_turn_started_at_falls_back_to_aggregator`,
`test_listen_lag_sampled_after_speech_stop_settles`); ruff + format clean; pyright at the
2-error baseline.

## Not covered

* Round 3 must re-verify `turn_started_at` on a long monologue (the overlap case) and
  `transcription_lag_secs` immediately after speech stops, plus the stretched scenario
  (second barge-in, deliberate 10 s silence, longer narration).
* The D10 claim rule inherits D2's positional bias until Task F emits empty-transcript
  events; not observable this round (no empty Whisper results occurred).
* Items 3–5 above are deliberately deferred to Task G with live reproductions in hand.
