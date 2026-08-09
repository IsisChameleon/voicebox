# Walkthrough — `feat/eval-fake-app`

*Status: **in progress**. Started 2026-08-09. Branched from `d07988b` (main, after PR #16 + #17
merged).*

Adds a self-contained fake web voice app ("Nova", a space-exploration trivia host) under
`tests/eval/fake_app/`, so anyone cloning the repo has a live conversational target for voicebox —
plus a ground-truth event log and a scripted-brain seam, making the fake a reference instrument for
future voicebox evals. Spec (the contract, all decisions resolved):
[`docs/specs/2026-08-09-demo-voice-app-for-dogfooding.md`](../specs/2026-08-09-demo-voice-app-for-dogfooding.md).

The app is a plain pipecat pipeline behind pipecat's development runner (SmallWebRTC prebuilt
User Interface on `:7860`). It knows nothing about voicebox; ground truth goes to disk only.

## Status

| Phase | What it changes | Commits | Evidence | Done |
|---|---|---|---|---|
| **0** | Spec committed; walkthrough opened | `15d223b` | — | ✅ |
| **1** | `pyproject` `eval` extra; `tests/eval/fake_app/{bot,brain}.py` + `prompt.md`; ground-truth JSONL observer; app serves prebuilt UI on `:7860` | `38184f4` | [t1-fake-app-phase1.md](../artefacts/feat-eval-fake-app/t1-fake-app-phase1.md) | ✅ |
| **2** | Dogfood S2–S4 live with voicebox against `http://localhost:7860` (round-trip events, turn-taking metrics with `record_dir`, barge-in) | | | |
| **3** | `tests/eval/fake_app/README.md`; stale readme-app / `localhost:3000` reference cleanup in `README.md` + `CLAUDE.md` | | | |

Scenario S1 (fresh clone → real-mic conversation with Nova) is 🔴 manual — it needs Isabelle at
the microphone and is **not** claimed verified by this branch until she runs it.

## Try it

```bash
uv sync --extra eval
export ANTHROPIC_API_KEY=...   # from this repo's .env
uv run python tests/eval/fake_app/bot.py
# then open http://localhost:7860 and click Connect
```

## Phase 1 notes

- Pipeline: `SmallWebRTCTransport → VADProcessor(Silero) → WhisperSTTService → user aggregator →
  brain → KokoroTTSService(voice_id="am_michael") → transport.output() → assistant aggregator`
  (`tests/eval/fake_app/bot.py:137-148`). All pipecat 1.3.0 API names verified against the
  installed package (`InterruptionFrame`, not the removed `StartInterruptionFrame`;
  `PipelineWorker` + `WorkerRunner`).
- Brain seam (`tests/eval/fake_app/brain.py`): `create_brain()` picks by
  `VOICEBOX_FAKE_APP_LLM_PROVIDER`; `scripted` plays 5 canned lines (line 3 is a deliberately
  long Luna 9 monologue for barge-in tests); missing key → `SystemExit` with a clear message.
- Ground truth: `GroundTruthObserver` appends JSONL to `temp/fake_app/ground_truth.jsonl` —
  `bot_speech_started/stopped`, `heard_user`, `brain_reply`, `interrupted`. Disk only.
- Implementation choices the spec left open (flagged, not silently decided): openai with no
  model env falls through to pipecat's own default; pipecat broadcasts `InterruptionFrame` on
  every user turn start, so `interrupted` records are real barge-ins only inside bot speech
  spans (noted in the observer docstring); ground-truth path is cwd-relative (run from repo
  root); no Kokoro `warm_up()` (cold start only delays the first greeting).

## Not covered (running list)

- S1 real-mic run — pending Isabelle.
- `ANTHROPIC_API_KEY` absent from this environment (no `.env` in the repo): the
  `anthropic`/`openai` brains are code-verified only; live runs use `scripted` until a key
  lands in this repo's `.env`.
- `ground_truth.jsonl` contents — observer only runs once a WebRTC client connects (Phase 2).
