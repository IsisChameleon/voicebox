# Review pass — branch diff, three independent lenses

*Captured 2026-08-04, after round 7, before proposing the PR. Fix commit `166ae5e`
(decisions `BUILDLOG.md` D20, D21). Scope: `git diff eb89647..HEAD` — 52 files,
~6.6k insertions. `/code-review` is user-invocation-only, so the equivalent pass ran as
three parallel read-only reviewer agents with distinct lenses (cross-flow consistency /
core-logic correctness / tests-and-docs coherence), findings verified against code and
pipecat 1.3.0 source before acting.*

## Fixed (commit `166ae5e`)

| Finding (lens) | Disposition |
|---|---|
| **Combined `wait_for_turn` + `wait_for_playout` got a flat 150 s IPC deadline** — the `if/elif` chain dropped D19's per-word allowance; a long text after a long turn wait surfaced as an IPC `TimeoutError` instead of the agent's diagnosis (found independently by ALL THREE lenses) | `_speak_deadline()` extracted; gates now compose (`150 + 0.8×words`); D21 |
| **`_Playout` cross-attribution** — an unwaited speak or fired barge-in still in flight when a waited speak installed its tracker resolved the waited speak with the PRIOR utterance's playout end (`played: true` with wrong timestamps) | count-based skip of pending `TTSStoppedFrame`s (one speak = one stop, D17's invariant); `_tts_pending` counter, reset on interruption; D20; pinned by `test_playout_ignores_prior_utterances_frames` |
| **Cross-process invariants pinned by nothing** — mirrored literals (0.8/word, 210 s) and `TURN_STOP_TIMEOUT_SECS > DRAIN_CAP_SECS` held only by comments; the existing watchdog test's floor was 60 s, *below* the cap it must outlive | `tests/test_server_deadlines.py` (4 pins) + strengthened watchdog assert; D21 revises r4's "no parent-side test seam" note |
| **Fire-and-forget tasks** (`warm_up`, `_start_recording`, armed triggers) — no strong refs (GC-able mid-flight), failures die as unretrieved exceptions; a crashed armed trigger was indistinguishable from one that never fired | `_spawn_task()` helper: strong refs + failure logging |
| **Docstring drift** — `listen()` omitted both barge-in event types and `transcription_empty`; `stop()` omitted `debug_log` and the `tester_think_time_gaps`/`outage_gaps` metrics keys; agent `speak()` omitted `played`/`reason`; `bot.py` contract lagged three shapes; `when`/`wait_for_turn` exclusivity unstated | all corrected in `server.py`/`agent.py`/`bot.py` |
| **`metrics.py` bias note** claimed `tester_speech_stopped` is "playout-accurate"; it trails true audio end by ~0.35 s (pipecat `BOT_VAD_STOP_SECS`) | note corrected |
| **CLAUDE.md / README drift** — old `listen`/`speak` return shapes, file map missing `timing.py`/`nonblocking_whisper_stt.py`/`events.py`/`metrics.py`, seven stale line refs (incl. `agent.py:449` added stale by this branch and copied into a test comment) | all updated (docs commit) |

## Recorded, deliberately not fixed

* **`turn_started_at` drift when smart-turn holds a turn open across VAD cycles** (logic
  lens, major): a mid-utterance pause judged INCOMPLETE enqueues 2 VAD starts for 1
  transcript, leaving a stale deque entry forever — verified against pipecat's
  `UserTurnController`. This is a third confirmed route into the desync already recorded by
  D17 (aggregator merge) and round 7 (mega-transcript); the fix is the per-segment
  transcript emission redesign the execution spec already names as its own design task.
  Heuristic pruning here would re-introduce D9-style guessing.
* **Router-respawn race in `agent_ipc.py`** (both consistency and logic lenses, minor,
  transient): a stale response-router can spuriously fail the first command of a *new*
  session started while the old router is inside its 0.5 s poll. `agent_ipc.py`/`bot.py`
  are **unchanged on this branch**; recorded for a follow-up rather than widening the diff.
* **`started_at` inheritance in `_Playout`**: when audio runs continuously from a prior
  utterance into the waited one, the start can still be the prior utterance's (frames carry
  no identity). D20 fixes the resolution correctness; the residual is documented in the
  class docstring.
* Pre-existing, out of scope: pyright's 2-error baseline (both shapes exist at `eb89647`);
  license headers absent on the 8 pre-existing test files (de-facto tests-exempt
  convention — `test_listen_ordering.py` and `test_server_deadlines.py` carry them);
  real-sleep test margins (probabilistic flake risk, all passing); `notes.md:21` referencing
  the D7-deleted `scripts/e2e_readme_call.py`; `KokoroTTSService` unit tests downloading the
  ONNX model on a cache-less machine; kokoro `stop_ttfb_metrics()` double-call (idempotent).

## Verified clean by the reviewers

Mirrored literals all match; the rest of the timeout chain orders correctly; event naming
consistent across `events.py`/`agent.py`/`metrics.py`; `nonblocking_whisper_stt.py` worker
ordering/drain/lag race-free; TOKEN one-speak-one-`run_tts` verified against pipecat's
aggregation source; `listen_events` sort/cursor under concurrent `_emit`; `metrics.py` edge
cases (empty logs, post-stop transcripts, outage boundaries); `stop()` sequencing; no
tautological tests; monkeypatched constants target the right modules; no committed
artifacts or orphan imports.

## Quality gates after the fix pass

```
uv run pytest -q                        → 76 passed   (71 → 76)
uv run ruff check src/ tests/           → All checks passed!
uv run ruff format --check src/ tests/  → 23 files already formatted
uv run pyright src/                     → 2 errors (the pre-existing baseline)
```
