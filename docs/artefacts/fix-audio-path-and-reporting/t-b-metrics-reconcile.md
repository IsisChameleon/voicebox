# Task B — `metrics.json` reconciles with itself

*Evidence for the success criteria in
[`docs/specs/2026-07-31-fix-plan-execution.md`](../../specs/2026-07-31-fix-plan-execution.md)
§ "Task B — `metrics.json` reconciles with itself".*

Captured 2026-08-01. Matching-rule decision: `BUILDLOG.md` D2.

## Criteria → evidence

| # | Criterion | Evidence |
|---|---|---|
| 1 | `_turns()` built from speech intervals; unmatched turn carries `transcript_missing`, no `text` | `metrics.py:262-289`, `test_turns_count_matches_utterances` |
| 2 | turn count == `utterances` by construction, asserted on both captured logs | before/after table below, `test_turns_count_matches_utterances_on_captured_sessions` |
| 3 | `response_latency_secs` rides on the turn, surviving a missing transcript | `test_latency_survives_missing_transcript` + table below |
| 4 | Gaps split by who owed the next turn; `total_dead_air_secs` = app dead air only | `metrics.py:292-318`, partition check below |
| 5 | A `biases` note names what the gap fields do and do not mean | `metrics.py:161-166`, `test_biases_note_covers_gap_attribution` |
| 6 | Existing 14 tests updated, none deleted | function-count check below |

## Before / after on the two captured sessions

Real event logs from dogfood runs against the readme app on 2026-07-28
(`tests/fixtures/`). "Before" is `git show 1df7e51:src/voicebox/metrics.py` run over the same
input, so the two columns differ only by this task.

```
--- session-20260728-121027-events.json
  BEFORE turns/utterances : {'app_bot': (2, 3), 'tester': (1, 1)}
  AFTER  turns/utterances : {'app_bot': (3, 3), 'tester': (1, 1)}
  BEFORE total_dead_air_secs : 98.123
  AFTER  total_dead_air_secs : 3.754   + tester_think_time 94.369   = 98.123
  AFTER  turns flagged transcript_missing : 1 (of which 1 still carry response_latency_secs)

--- session-20260728-cyoa-choice-events.json
  BEFORE turns/utterances : {'app_bot': (8, 11), 'tester': (3, 6)}
  AFTER  turns/utterances : {'app_bot': (11, 11), 'tester': (6, 6)}
  BEFORE total_dead_air_secs : 302.276
  AFTER  total_dead_air_secs : 11.407   + tester_think_time 290.869   = 302.276
  AFTER  turns flagged transcript_missing : 6 (of which 0 still carry response_latency_secs)
```

Reading this:

- **Criterion 2.** Every `(turns, utterances)` pair now matches. Session 2 was reporting 8 app-bot
  turns for 11 utterances and 3 tester turns for 6 — three app utterances and three tester
  utterances simply absent from the report.
- **Criterion 3.** Session 1's single missing transcript is the 44.8 s app-bot turn from the field
  report. Its measured `response_latency_secs: 3.754` used to vanish with the text; the artefact
  now shows the flagged turn **still carrying it**.
- **Criterion 4.** The old `total_dead_air_secs` of 98.1 s / 302.3 s read as "the app under test
  was silent for 98 seconds", which was never true — nearly all of it was the driving agent
  thinking between `speak()` calls. Real app dead air is **3.754 s** and **11.407 s**. The two new
  fields sum exactly to the old number (98.123 and 302.276), so the change is a re-attribution of
  the same silence, not a recount.

## Test run

```
$ uv run pytest -q
.............................                                            [100%]
29 passed in 6.87s
```

Criterion 6 — no test deleted to make a failure go away:

```
$ git show 1df7e51:tests/test_metrics.py | grep -c "^def test_"
14
$ grep -c "^def test_" tests/test_metrics.py
22
```

All 14 original names are still present; 8 were added.

## The one test that changed its expectation

`test_turns_count_matches_utterances` asserted `["one", None, "three", None]` for a session with a
dropped middle transcript. The implementation produces `["one", "three", None, None]`. The test
was corrected, not the code — full reasoning in `BUILDLOG.md` D2. In short: the expected list is
produced by no coherent rule (claiming from the left gives `["one", "three", None, None]`, from
the right `["one", None, None, "three"]`), and the test had drifted from the spec story it cites.

The behaviour it was groping at is real and is now pinned by its own test,
`test_dropped_transcript_shifts_text_onto_the_next_turn`, matching the bias already documented in
`metrics._BIAS_NOTES`.

## Quality gates

```
$ uv run ruff check src/ tests/
All checks passed!

$ uv run ruff format --check src/ tests/
15 files already formatted

$ uv run pyright src/
  src/voicebox/agent.py:407:42 - error: "start_recording" is not a known attribute of "None"
  src/voicebox/browser_session.py:35:23 - error: Variable not allowed in type expression
  2 errors, 0 warnings, 0 informations
```

Both pyright errors are pre-existing and outside this task's files — provenance established in
[`t-a-startup-fails-loudly.md`](t-a-startup-fails-loudly.md) and
[`t-c-timing-instrumentation.md`](t-c-timing-instrumentation.md).

## Not covered

- **Transcript→interval matching is positional, not semantic.** A dropped or out-of-order
  transcript shifts text onto a neighbouring turn. Documented and pinned, not fixed — see D2.
- **`total_dead_air_secs` still inherits the VAD bias.** `app_bot_speech_stopped` trails true
  speech end by ~`vad_stop_secs`, so app talk time is overestimated and the think-time gap that
  follows a bot turn is understated by ~1 s per turn. Task D moves the VAD upstream and will
  change every one of these numbers again.
- **Overlapping speech.** When both parties are talking, a gap is attributed to whichever
  interval ends furthest right. That is a convention, not a measurement; no test covers a gap
  that opens after a talk-over window.
- Nothing here is live-verified. These are replays of captured logs — no browser, no app.
