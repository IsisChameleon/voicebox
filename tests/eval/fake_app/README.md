# Nova — the in-repo fake voice app

A self-contained web voice app ("Nova", a space-exploration trivia host) so voicebox always has
something to talk to: clone the repo, start it with one command, and run a live dogfood session
against it. It also doubles as a reference instrument for future evals — it writes its own view
of the conversation (ground truth) that voicebox's `listen()` claims can be scored against.

It is NOT a product — no auth, no persistence, no multi-session support — and it knows nothing
about voicebox. It's a plain pipecat voice app (browser UI + SmallWebRTC, Silero VAD, Whisper
STT, a swappable LLM "brain", Kokoro TTS with a different voice than the tester), driven exactly
as voicebox would drive any third-party app.

## Quickstart

```bash
uv sync --extra eval                       # pulls pipecat's webrtc + anthropic extras
export ANTHROPIC_API_KEY=...               # or use the key-free scripted mode below
uv run python tests/eval/fake_app/bot.py   # run from the repo root
```

Open http://localhost:7860, click **Connect**, and talk to Nova with your real mic.

Deterministic, key-free run:

```bash
VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted uv run python tests/eval/fake_app/bot.py
```

## Configuration (env vars, all optional)

| Var | Default | Notes |
|---|---|---|
| `VOICEBOX_FAKE_APP_LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `scripted` — picks the brain (`brain.py`) |
| `VOICEBOX_FAKE_APP_LLM_MODEL` | `claude-haiku-4-5` | any model id for the chosen provider |
| `VOICEBOX_FAKE_APP_STT_MODEL` | `base` | Whisper size; keep small — the voicebox tester runs its own Whisper |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | required for the LLM providers (not `scripted`); read from this repo's `.env` |
| `--port` (runner flag) | `7860` | no clash with voicebox's 9090/9091/9222 |

## Talking to Nova

Nova asks trivia in short turns (two sentences or fewer). Say **"explain in detail"** to get a
long multi-sentence answer — the reliable target for barge-in tests
(`speak(when="app_bot_speech_started", ...)`). The subject knob is `prompt.md`: edit it to change
what Nova quizzes about, no code change.

In `scripted` mode the five canned lines in `brain.py` play in order, one per user turn,
regardless of what you say; line 3 is the deliberately long one.

## Ground truth

Every session appends the app's own timeline to `temp/fake_app/ground_truth.jsonl` (anchored to
the repo root, whatever directory you launch from). One JSON record per line: session start,
bot speech start/stop, what its Whisper heard from the tester (`heard_user`), what the brain
replied (`brain_reply`), and interruptions (`during_bot_speech: true` marks a real barge-in). This is the reference timeline future
evals score voicebox's `listen()` events against — disk only, never exposed over the network, so
voicebox can't cheat.

## Pointing voicebox at it

Start this app, then call `start_browser_session("http://localhost:7860")` and have the
CDP-attached Playwright client click **Connect**. The shim's synthetic mic and audio tap handle
the rest — `listen()` returns Nova's turns as `app_bot_transcript` events.
