# CLAUDE.md — voicebox

Guidance for Claude Code working in this repo. Read this first; it captures the architecture and
the non-obvious traps so you don't have to re-derive them.

## What this is

An **MCP server that gives an LLM agent voice + ears inside a browser**, so the LLM can play a
synthetic *user* against any web voice app (Daily, LiveKit, plain `RTCPeerConnection`) without the
app knowing. The agent drives a Playwright Chromium with an audio shim injected: the shim fakes the
page's microphone (fed by Kokoro TTS from this server) and tees the bot's remote WebRTC audio back
to Whisper STT.

Local models by default — Whisper (STT) + Kokoro (TTS) — so **no API keys needed**.

## Architecture — three processes

```
Claude (LLM) ─HTTP/JSON-RPC─► voicebox MCP server (parent, server.py)
                                   │  multiprocessing.Queue (IPC)
                                   ├─► CHILD 1: pipecat audio agent (bot.py → agent.py)
                                   │            WebsocketServerTransport on :9091
                                   │            raw 16-bit PCM: Kokoro 48 kHz out, Whisper 16 kHz in
                                   └─► CHILD 2: Playwright Chromium (browser_session.py)
                                                --remote-debugging-port=9222 + shim.js injected
                                                parks until told to stop
```

- **Audio never crosses the MCP boundary.** MCP carries text/control only. Audio flows out-of-band
  over a raw-PCM WebSocket between `shim.js` (in the page) and the pipecat child.
- **UI driving is delegated to an EXTERNAL Playwright client** over CDP. voicebox exposes the CDP
  endpoint (`http://localhost:9222`) and the client (`@playwright/mcp --cdp-endpoint=...`, or
  `chromium.connect_over_cdp(...)`) logs in / clicks / navigates. voicebox itself has **no**
  `navigate`/`click`/`snapshot` tools, and the parent holds **no** Playwright handle to the page.

## File map

| File | Role |
|---|---|
| `src/voicebox/server.py` | FastMCP HTTP surface (parent). The 4 tools Claude calls. No audio code. Ports: MCP 9090, audio 9091, CDP 9222. |
| `src/voicebox/agent_ipc.py` | The parent↔pipecat-child mailbox: `multiprocessing.Queue` + child lifecycle. Uses `spawn` (not fork). |
| `src/voicebox/bot.py` | The pipecat child's command loop: `read → dispatch(listen/speak/stop) → respond`. |
| `src/voicebox/agent.py` | `PipecatMCPAgent` — owns the Pipecat pipeline behind `WebsocketServerTransport`. STT/TTS/VAD/turn config lives here. |
| `src/voicebox/runner_args.py` | `BrowserShimRunnerArguments` dataclass (host, port, mic_rate, tap_rate, record_dir). Pipecat ships none for plain WS-server transports. |
| `src/voicebox/raw_pcm_serializer.py` | Tiny `FrameSerializer`: raw 16-bit LE mono PCM, no protobuf/envelope. |
| `src/voicebox/processors/kokoro_tts.py` | Kokoro TTS service (`voice_id="af_heart"`). |
| `src/voicebox/shim.js` | The browser shim, injected via `addInitScript` before page code. Overrides `getUserMedia` (Hook 1) and wraps `RTCPeerConnection` (Hook 2). Diagnostics on `window.__voiceShim`. |
| `src/voicebox/browser_session.py` | Manages the Playwright child process. Supports `user_data_dir` (persistent default context, CDP-coherent) and `storage_state` (loaded into a separate context — auth works but invisible to / unsaveable by a CDP client; see trap below). Both exposed via `start_browser_session`. |
| `scripts/smoke_browser_shim.py` | Audio-path smoke test (no readme app needed). The reference for `connect_over_cdp` + reading `__voiceShim`. |
| `scripts/e2e_readme_call.py` | Full e2e driver: login → navigate → call → speak/listen → end, against the readme app. Canonical CDP-driving example. Runs `headless=True` and dumps WAVs. |

## MCP tools (`server.py`)

- `start_browser_session(url, headless=False, cdp_port=9222, audio_port=9091)` → `{cdp_endpoint, audio_ws_url}`
- `speak(text)` → queues Kokoro frames (returns when queued, NOT when audio finishes)
- `listen(timeout=30)` → VAD-segmented transcript string, `""` on timeout. **Returns text only — no timestamps.**
- `stop()` → tears down pipecat child + browser child

## Non-obvious facts & traps (verified, don't re-derive)

- **Asymmetric sample rates are intentional.** Browser→pipecat tap = **16 kHz** because
  `mlx_whisper.transcribe()` has no `sample_rate` param and hard-assumes 16 kHz; the shim's outbound
  `AudioContext` does the 48→16 resample. pipecat→browser mic = **48 kHz** to match the page's native
  AudioContext. See `runner_args.py` docstring, `agent.py:280-291`, `shim.js:31-37`.
- **VAD `stop_secs=1.0s`** (`agent.py:101`), not pipecat's default 0.2s — 0.2s chops remote WebRTC
  speech mid-sentence into single-word transcripts. Consequence: utterance "end" wall-clock lands
  ~1 s after speech truly stops.
- **The shim taps via Web Audio, NOT WebCodecs `MediaStreamTrackProcessor`** (`shim.js:156-304`).
  `MediaStreamTrackProcessor` drops silence on a remote track → sparse stream → WAV plays ~3× fast.
  Web Audio is pulled at a fixed rate and fills silence with zeros, preserving real-time pacing.
- **`RTCPeerConnection` track events are deduped by `track.id`** (`shim.js:181-246`) — Daily opens
  multiple peer connections and the same logical audio surfaces more than once (commits `26bd9c5`,
  `2b3d7f1`).
- **`enable_rtvi=False`** (`agent.py:133-137`) — we're a headless synthetic user; transcripts reach
  Claude via `listen()`'s return value, not RTVI data-channel notifications.
- **Timestamps ARE available but discarded.** `UserTurnStoppedMessage.timestamp` (turn start) is
  dropped at `agent.py:155`; `VADUserStartedSpeakingFrame`/`VADUserStoppedSpeakingFrame` carry
  wall-clock `time.time()` (pipecat `frames.py:1030-1057`). STT is batch+VAD-segmented, so true
  real-time *per-word* receive timestamps are NOT obtainable — only utterance-level wall-clock, or
  word offsets *within* a segment.
- **`record_dir` exists** (`runner_args.py`, `agent.py:105-128,194-231`): set it and `stop()` writes
  user/bot/merged WAVs via `AudioBufferProcessor`. Snapshot buffers BEFORE `stop_recording()` — it
  resets them.
- **`spawn`, not fork** (`agent_ipc.py:24`) — forking from the async MCP context copies the event
  loop / fds / locks and breaks.
- **Shim is defensive**: every hook is gated on the API existing; on insecure origins (non-localhost
  http, about:blank) or missing WebCodecs the hook is skipped silently. `window.__voiceShim` always
  exists with diagnostics (`installed`, `wsReady`, `inboundChunks`, `outboundChunks`, `errors`, …).
- **Known limitation:** the `RTCPeerConnection` wrap can't reach peer connections inside cross-origin
  iframes or Web Workers (e.g. Daily Prebuilt `<DailyIframe>`).
- **CDP context split (verified):** `chromium.launch()` + `new_context()` puts the shim page in a
  non-default browser context. A client attached via `connect_over_cdp` *sees the page* under
  `contexts[0]` but cookie ops (`context.cookies()`, `context.storage_state()`) hit the **default**
  context and come back empty. So `storage_state` LOAD works (the page sends the cookie — confirmed
  by echo test) but SAVE-via-CDP does not. `launch_persistent_context` (`user_data_dir`) uses the
  default context, so it's fully CDP-coherent — prefer it for session reuse.

## Dev workflow

```bash
uv sync                                   # install (uv, not pip; deps in pyproject.toml)
uv tool install -e .                      # install the `voicebox` CLI entry point
voicebox                                  # run MCP server on http://localhost:9090/mcp
uv run python scripts/smoke_browser_shim.py   # audio-path smoke test (no app needed)
uv run python scripts/e2e_readme_call.py      # full e2e against a localhost:3000 app
```

## Quality checks (run before committing)

```bash
uv run ruff check src/    # lint: docstring (D) + import (I) rules, line-length 100
uv run ruff format src/   # format
uv run pyright src/       # types
```

There is **no unit-test suite** — verification is via the two `scripts/` drivers, which need a
real browser (and, for e2e, a running voice app on `localhost:3000`).

## Conventions

- Python ≥ 3.11, `uv` for everything. Google-style docstrings (ruff `D` enforced).
- License header (BSD-2-Clause, "Copyright (c) 2026, Daily") at the top of every `.py` — copy the
  existing block when adding files.
- Single session at a time: ports 9090/9091/9222 are pinned unless overridden via tool args.
