# Round 7 — blind live verification (Task H live + D17/D16 confirmation)

*Captured 2026-08-04. Fix commit `3d965a7` (code), decision `BUILDLOG.md` D19.
Session artefacts: `temp/verify-round-7/` (blind session) and `temp/verify-round-7b/`
(targeted re-check of the D19 fix); server logs `temp/voicebox-server-round7*.log`
(gitignored). First round on the post-H code; ~22 min, 9 tester utterances, 2 app-side
wedge/reload cycles.*

## What held up (the round's verification targets)

* **D17 TOKEN aggregation, live at last:** every multi-sentence speak — including the
  61-word 3-sentence opener — played as exactly **one** `tester_speech_started/stopped`
  pair (opener: 18.4 s continuous span, t=…576.476→…594.903). No mid-utterance splits
  anywhere in the session; the phantom `transcript_missing` tester rows are gone from
  `metrics.json`. D16's warm-up holds: the *first* speak of the session was single-span.
* **`wait_for_playout` spans the whole utterance** (Task G + D17): speak #4
  `{started_at: …133.493, finished_at: …145.843, interrupted: false}` — a 12.35 s span
  matching the event log exactly. (The opener's `played: false` is the round's one real
  defect — below.)
* **D18 `waited_for_turn_secs`, three live observations:** `0.0` when Ember was silent;
  **17.748 s** mid-monologue — arithmetically exact against her `app_bot_speech_stopped`
  (…188.001 − wait start ≈ 17.75); `2.827 s` when she had just restarted. The tester
  called the polite path "exactly what the doc says".
* **Task H live:** cursor paging flawless over 30 `listen()` calls including across two
  page-reload reconnects — no event skipped or repeated. The sort itself was never
  stressed: **0 append-order inversions in all 107 events** (`events.json`), i.e. the
  D/E fixes shrank the skew to nothing this session, exactly the "not covered" prediction
  in `t-h-listen-ordering-docstrings.md`. Sorting is confirmed harmless, unexercised as
  a repair.
* **F1 again:** a prompt `stop()` (~4.6 s after final playout) blocked 44.5 s, drained,
  and the final transcript landed after `session_stopped` in `events.json` (index 106 of
  106) — as the new `stop()` docstring warns.
* **Barge-in:** fired at trigger **+1.500 s** exactly (`triggered_by_t` = the
  `app_bot_speech_started` it keyed on), correctly skipped the turn already in progress
  at arm time ("NEXT occurrence" honoured), audio on the wire ~4.0 s after fire — inside
  the documented 3–9 s synthesis band.
* **Metrics quarantine (D12):** two app wedge/reload cycles booked as
  `total_outage_secs: 361.308`, keeping `mean_app_response_latency_secs` at an honest
  5.237 (max 6.113).

## Found and fixed this round (commit `3d965a7`, D19)

**The long-first-utterance `played: false` footgun.** The by-the-book 61-word opener with
`wait_for_playout=True` returned `played: false` on a perfectly healthy playout: TOKEN
mode synthesizes the whole utterance before any audio plays, so playout started at
+17.8 s and *ended* at +36.2 s — past the flat `PLAYOUT_TIMEOUT_SECS = 30` window. The
`reason` text was accurate (the tester confirmed the audio via `listen()`), but the flag
lied on exactly the long-opener shape rounds 5–6 made the recommended pattern. Fix:
window = `30 + 0.8 s × words` (~2× the measured ~0.3 s/word audio rate), mirrored into
`server.py`'s IPC deadline (`60 + 0.8 × words`, D15 ordering preserved). Pinned by
`test_playout_window_scales_with_text_length`.

**Live re-check after the fix (`temp/verify-round-7b`):** same 3-sentence ~60-word text,
`wait_for_playout=True`, on the restarted server:

```
{"queued": true, "played": true,
 "started_at": 1785807526.079, "finished_at": 1785807546.179, "interrupted": false}
tester_transcript t=1785807518.250 → queue→finish 27.9 s, one speech pair
```

27.9 s on an *idle* box — under the old flat window by 2 s only; round 7's STT
contention pushed the same shape to 36.2 s. The scaled window for this text is 77.2 s.

## Closed as not-a-defect, with parent-side proof (extends D17's closure)

The tester measured speak/arm calls "taking 4.5–9 s to return" (arm: issued 11:16:50.589,
returned 11:16:59.464). Ground truth across both logs: the parent processed the arm's
`CallToolRequest` at **11:16:56** (`voicebox-server-round7.log`), the child logged receipt
at **11:16:56.607** and emitted `tester_barge_in_armed` at **.608** — the entire
server-side path is ~10 ms. The 6 s gap is the caller's own LLM turnaround between
reading its clock and the HTTP request landing. Same pattern on all 9 speaks. The D17
guidance (arm early, arming is instant) stands, now proven at the parent boundary too.

Related client-side observation: the tester reported `waited_for_turn_secs`,
`transcription_lag_secs` etc. as "not in the docstrings I was given" — its MCP client had
cached the tool list from before the server restart (this session's own ToolSearch showed
the same stale cache). Docstring changes need a client reconnect to be seen; not a
voicebox defect, worth knowing when reading tester reports.

## Known and documented, not fixed here

* **The lag blind spot bit, live, exactly as now documented:** `transcription_lag_secs`
  collapsed to 0.0 twice while a ~2,400-char merged transcript was still held by the
  aggregator's open turn, arriving ~6 min late (t=…682.156) with a stale
  `turn_started_at` — the tester mis-diagnosed Ember as unresponsive during a wedge she
  had actually answered. This is D11/D14's documented blind spot plus D17's deferred
  aggregator merge; decisive further evidence for the per-segment-transcript redesign,
  not fixable inside this branch.
* **11 hallucinated `app_bot_transcript` events** ("Thank you." ×9, "Thank you. Thank
  you.", trailing "Sorry. Thank you.") from near-silent 1–2 s VAD flap segments during an
  app wedge — Whisper model behaviour on WebRTC noise (known since round 5's quality
  nits); a duration/confidence filter is recorded as a possible follow-up, out of this
  branch's scope (the spec pins the STT config; issue #13 carries decode-side work).
* `stop()` at 44.5 s and the drained-after-`session_stopped` transcript: both now
  documented behaviour, observed matching their docstrings.

## Quality gates

Suite 70 → 71 passing; ruff + format clean; pyright at the 2-error baseline.

## Not covered

* The H sort has still never repaired a real out-of-order batch live (no skew left to
  repair); it remains pinned by unit tests only.
* The scaled playout window is verified at ~60 words idle; not exercised at the extreme
  (150+ words under decode contention).
* App-side bugs (wedges, auto-toggling mic button) remain readme issue #182 territory;
  both reloads recovered cleanly through the shim (`client_disconnected/connected`
  pairs, session survived).
