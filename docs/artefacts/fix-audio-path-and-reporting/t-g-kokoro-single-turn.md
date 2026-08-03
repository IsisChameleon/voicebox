# Task G — Kokoro plays one utterance, not several

*Evidence for the success criteria in
[`docs/specs/2026-07-31-fix-plan-execution.md`](../../specs/2026-07-31-fix-plan-execution.md)
§ "Task G — Kokoro plays one utterance, not several".*

Captured 2026-08-03. Commit `57d9527`.

## Criteria → evidence

| # | Criterion | What landed | Evidence |
|---|---|---|---|
| 1 | `run_tts` buffers the whole utterance before yielding audio | stream consumed to completion into `chunks`, then frames yielded back-to-back | `test_utterance_yields_single_audio_span` (chunks arrive with 0.1 s synthesis gaps; every audio frame is yielded after the last chunk was produced, inter-yield spans < 50 ms) |
| 2 | `TTSStartedFrame`/`TTSStoppedFrame` still bracket; error path keeps the `finally` guarantee | unchanged `try/except/finally` shape | `test_tts_frames_still_bracket_and_error_path_closes` (including a mid-stream `RuntimeError`) |
| 3 | `_Playout` resolves on the first `BotStoppedSpeakingFrame` following the utterance's `TTSStoppedFrame` | `on_tts_stopped()` fed by the pipeline observer (which now watches `TTSStoppedFrame`, no event emitted); `on_stopped()` resolves only once `_tts_finished` | `test_playout_resolves_on_bot_stopped_after_tts_stopped` (a bot pause *before* synthesis finished does not resolve) |
| 4 | `PLAYOUT_SETTLE_SECS` + settle machinery deleted, docstrings too | constant, timer handle, `settle_secs` param and both docstrings gone | `grep -rn PLAYOUT_SETTLE src/ tests/` → no hits |
| 5 | Interruption still resolves immediately | `on_interrupted` unchanged (minus timer bookkeeping) | `test_interruption_resolves_immediately` |

## Why this is the fix and not a tuning

Live reproductions this branch collected before the fix:

* Round 3: a 4.2 s synthesis gap mid-sentence made EmberTales endpoint on the silence, take
  the turn, get talked over by the resuming fragment, and scold the tester ("I appreciate
  your enthusiasm, but…") — an invalidated run, exactly story G1's rationale.
* Round 2: `wait_for_playout` resolved at the first burst boundary, `finished_at` 4.1 s
  before the audio really ended (the second burst started 1.02 s later — just past the old
  1.0 s settle window). Criterion 3's event-driven resolution replaces that guess.
* Rounds 2–3: every split's second interval became a phantom `transcript_missing` tester turn in
  `metrics.json` (3 per session). With one gap-free span per utterance those disappear at the
  source.

## Test run

```
$ uv run pytest -q tests/test_kokoro_playout.py
....                                                                     [100%]
4 passed in 4.69s

$ uv run pytest -q
63 passed, 4 warnings in 51.94s

$ uv run ruff check src/ tests/   → All checks passed!
$ uv run ruff format --check src/ tests/ → 21 files already formatted
```

`uv run pyright src/` at the 2-error baseline (unchanged).

## Not covered

* **G4 🔴 (live-only)** — that the app's own logs show one user turn per `speak()`. Queued
  for the post-G blind verification round.
* **`speak()`-to-audio latency rises by design**: audio now starts only after the whole
  utterance is synthesized. Round-3 numbers (1.6–5.4 s) were already full-synthesis-bound
  for short sentences, so the delta should be small; the live round must confirm it stays
  tolerable for long tester sentences.
* **The barge-in synthesize-at-arm idea** (fire → cached audio plays instantly, making
  `timer_secs` mean what it says) is NOT part of this task — recorded in the r3 artefact as
  a candidate follow-up; `timer_secs` still under-shoots by the synthesis time.
* The real Kokoro model is loaded in tests but synthesis is stubbed — chunk pacing is
  simulated, not measured from the ONNX runtime.
