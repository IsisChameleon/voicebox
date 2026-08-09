# Fake voice app for dogfooding & eval ("Nova" trivia host)

**Status: DRAFT v2 — decisions 1–4 resolved (2026-08-09), eval-readiness section added.
No code written yet; implementation waits until the in-flight fixes on
`fix/decouple-transcript-delivery` land.**

## Problem

voicebox's end-to-end behaviour can only be proven against a live web voice app. The old
"readme app" on `localhost:3000` no longer exists, and the current live target (EmberTales)
is an external app with its own bugs, credentials, and availability problems. Any potential
voicebox user cloning this repo today has **no app to point voicebox at** — the smoke scripts
exercise the audio path but nothing conversational.

Goal: a self-contained fake voice app in this repo that anyone can start with one command and
use as the target of a live voicebox session. It holds a conversation via a configurable,
cheap LLM.

## Non-goals

- Not a product. No auth, no persistence, no UI work, no multi-session support.
- Not a replacement for `scripts/smoke_*.py` (those stay; they test the audio path without an app).
- No local-LLM fallback (Ollama etc.) in v1 — the audio stack is local; the LLM needs one API key.
- No test-harness features inside the app (voicebox is the harness; the app stays "any web voice app").

## Architecture shape

**Pipecat pipeline behind pipecat's development runner** — the same layered-pipeline pattern as
voicebox's own `agent.py`, but as an independent third process with a real WebRTC front door:

```
Browser (prebuilt UI, http://localhost:7860)
   │  getUserMedia (mic up) + RTCPeerConnection (bot audio down)   ← the two surfaces shim.js hooks
   ▼
SmallWebRTCTransport
   → SileroVAD → WhisperSTTService (base/tiny) → user aggregator
   → LLM service (default: AnthropicLLMService, claude-haiku-4-5)
   → KokoroTTSService (reused from voicebox, different voice than the tester)
   → transport output
```

- **Zero frontend code.** `pipecat.runner.run` serves the `pipecat_ai_prebuilt` client at `:7860`
  and handles the WebRTC offer/answer. Verified against installed pipecat 1.3.0.
- **The demo app knows nothing about voicebox.** It is a plain pipecat voice app; voicebox drives
  it exactly as it would any third-party app (start_browser_session → CDP client clicks Connect).
- **Reuse, not parallel flows:** TTS reuses `voicebox.processors.kokoro_tts.KokoroTTSService`
  (different `voice_id`, e.g. `am_michael`, so tester and bot are audibly distinct in recordings).
  STT uses pipecat's stock `WhisperSTTService` — the bot does *not* need voicebox's
  `NonBlockingSegmentedSTT` machinery; keeping stock parts keeps the app representative of a
  real third-party app.

### Verified load-bearing assumptions (this session, pipecat 1.3.0 installed)

| Claim | Evidence |
|---|---|
| Runner supports SmallWebRTC + prebuilt UI on :7860 | `pipecat/runner/run.py` — `SmallWebRTCRequestHandler`, `PipecatPrebuiltUI`, `RUNNER_PORT: int = 7860` |
| Prebuilt client calls `getUserMedia` + `RTCPeerConnection` from **main-frame** scripts | greps over `pipecat_ai_prebuilt/client/dist/` — hits in `index.module-*.js`; zero `<iframe`/`new Worker(` |
| SmallWebRTC needs the `webrtc` extra | import error: "pip install pipecat-ai[webrtc]" (aiortc missing today) |
| Anthropic LLM service exists, needs `anthropic` extra | `pipecat.services.anthropic.llm` import fails only on missing `anthropic` package |
| `OpenAILLMService` already importable | present in current env |

### Still 🔴 live-only (cannot be proven statically)

- The shim actually captures the prebuilt page's audio (`__voiceShim.inboundChunks/outboundChunks > 0`).
- Resource load: two Whisper + two Kokoro instances on one machine — watch `transcription_lag_secs`.
- `uv sync --extra eval` resolves cleanly (aiortc + anthropic added to the tree).

## The bot: "Nova", space-exploration trivia host

Chosen to exercise voicebox's surface, not for charm:

- **Short structured turns** (question → wait → react, ≤ 2 sentences) → clean turn-taking metrics.
- **On request "explain in detail"** the prompt allows a long answer → a reliable long utterance
  for `speak(when="app_bot_speech_started", ...)` barge-in tests.
- System prompt lives in `tests/eval/fake_app/prompt.md` (edit to change subject — no code change).

## Configuration (env vars, all optional)

| Var | Default | Notes |
|---|---|---|
| `VOICEBOX_FAKE_APP_LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `scripted` — picks the brain (see Eval-readiness) |
| `VOICEBOX_FAKE_APP_LLM_MODEL` | `claude-haiku-4-5` | cheapest current Claude ($1/$5 per MTok); any model id for the chosen provider |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | required for LLM providers (not `scripted`); read from this repo's `.env` |
| `VOICEBOX_FAKE_APP_STT_MODEL` | `base` | Whisper size; keep small — the voicebox tester runs its own Whisper |
| `--port` (runner flag) | `7860` | no clash with voicebox's 9090/9091/9222 |

Provider is configurable via pipecat's service classes (both wrap the provider's official SDK).
Default is Anthropic per the "cheap one" requirement — Claude Haiku 4.5.

## Layout & packaging

```
tests/eval/fake_app/
  bot.py        # pipeline assembly + runner entry (~150 lines, license header, ruff-clean)
  brain.py      # the "brain" seam: LLM service factory + scripted responder (see Eval-readiness)
  prompt.md     # Nova's system prompt (the "subject" knob)
  README.md     # quickstart: uv sync --extra eval → export key → uv run python tests/eval/fake_app/bot.py
pyproject.toml  # [project.optional-dependencies] eval = ["pipecat-ai[webrtc,anthropic]>=1.3.0"]
```

- **Decision (2026-08-09): lives under `tests/eval/`** — it is a (sophisticated) fake, i.e. a test
  fixture, not a product demo, and `tests/eval/` is the umbrella where the future eval harness
  (scenario runner, aligner, scoring) will land beside it. Safe under `tests/`: pytest only
  collects `test_*.py`, so `bot.py` is never imported by the unit suite, and ruff's `tests/**`
  per-file-ignores apply.
- **Decision (2026-08-09): the extra is named `eval`** (not `demo`/`fake-app`) — it will
  accumulate the eval harness's dependencies too, so the broader name avoids a rename later.
- Optional extra keeps aiortc/anthropic out of the core install; core `voicebox` is unaffected.

## Scenarios → acceptance (4+1 delta)

| # | Scenario | Accepted when |
|---|---|---|
| S1 | **Onboarding**: fresh clone, `uv sync --extra eval`, export `ANTHROPIC_API_KEY`, `uv run python tests/eval/fake_app/bot.py` | browser at `:7860` → Connect → talk to Nova by real mic. 🔴 manual |
| S2 | **Dogfood round-trip**: voicebox `start_browser_session("http://localhost:7860")`, CDP client clicks Connect, `speak` an answer, `listen` | events show `app_bot_transcript` of Nova's reply; `__voiceShim` counters non-zero. 🔴 live |
| S3 | **Turn-taking**: full trivia exchange, `stop()` with `record_dir` | `metrics.json` turn latencies present and sane; WAVs contain both voices. 🔴 live |
| S4 | **Barge-in**: ask Nova for a detailed explanation, arm `speak(when="app_bot_speech_started", timer_secs=2)` | `tester_barge_in_fired` then `app_bot_speech_stopped` shortly after. 🔴 live |

**View impact:** logical — none (voicebox source untouched except the Kokoro import is now shared);
development — new `tests/eval/fake_app/` dir + one pyproject extra; process — a third independent OS process;
physical — port 7860. No changes to MCP tools, events, or metrics.

Being a demo app, `pytest`-able surface is near nil (maybe env parsing); evidence for the
🔴 scenarios is a dogfood transcript + artefact per walkthrough discipline.

## Eval-readiness (future: agentic system eval of voicebox)

voicebox is a measurement instrument — its `listen()` events, transcripts and `metrics.json` are
*claims about what happened in the conversation*. Evaluating voicebox therefore means scoring
those claims against **ground truth**, and a fake app we control is the only place ground truth
can come from. That reframing changes two things about v1 and defers the rest:

### Build into v1 (cheap now, expensive to retrofit)

1. **Ground-truth event log.** The fake app attaches a pipecat observer (same `BaseObserver`
   pattern as `agent.py`'s) that writes a timestamped JSONL to `temp/fake_app/ground_truth.jsonl`:
   bot speech start/stop (from its own TTS/playout frames), what its Whisper heard from the tester,
   what the brain replied, interruptions. An eval run then aligns voicebox's *observed* timeline
   against the fake's *emitted* one: event-detection recall, latency error distributions,
   transcript WER — without this log the fake is only a demo, with it it's a reference instrument.
   ~30 lines, and it doubles as a debugging aid during dogfooding.
2. **A swappable "brain" seam.** The pipeline slot currently filled by `AnthropicLLMService` is
   factored behind a tiny factory (`brain.py`) so a **scripted responder** can slot in:
   `VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted` plays deterministic canned responses (optionally with
   fixed think-delays). Evals need determinism — LLM nondeterminism turns every metric into noise —
   and the scripted mode also removes the API-key requirement for repeatable/CI-ish runs. In
   pipecat this is literally "which FrameProcessor goes in this position", so the seam costs a
   function, not an abstraction layer. The LLM modes stay for open-ended dogfooding.

### Explicitly deferred (design accommodates, v1 does not build)

- Fault-injection knobs (artificial response delay, mid-utterance pauses, silence, overlong
  monologues) to sweep voicebox's detection envelope — natural extensions of the scripted brain.
- The eval harness itself (scenario runner, aligner, scoring vs `ground_truth.jsonl`) — a separate
  design pass via the `eval-runner` skill once the fake exists.
- Multi-scenario script library.

What does **not** change: the app stays a plain pipecat app that knows nothing about voicebox —
ground truth is written to disk, not exposed to the tester; voicebox must not be able to "cheat".

## Decisions (resolved 2026-08-09)

1. **Location**: under `tests/eval/fake_app/` — it's a fake/fixture; `tests/eval/` is the
   umbrella for future eval tooling. The dependency extra is named `eval` for the same reason.
2. **Voice**: `am_michael` for Nova.
3. **Stale readme-app references** in README / CLAUDE.md: cleaned up in the same branch (Phase 3).
4. **Port**: pipecat default `7860`.

## Execution plan (after in-flight fixes land; new branch)

1. Phase 1 — pyproject `eval` extra + `tests/eval/fake_app/{bot,brain}.py` + `prompt.md` +
   ground-truth observer; verify `uv sync --extra eval` and S1 with a real mic. Commit.
2. Phase 2 — dogfood S2–S4 with voicebox; capture evidence artefact + walkthrough row. Commit.
3. Phase 3 — `tests/eval/fake_app/README.md` + stale readme-app reference cleanup in README/CLAUDE.md.
   Commit, /code-review `medium`.
