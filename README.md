# voicebox

# voicebox

**Give your coding agent a voice and ears so it can test your voice agent for you.**

Testing a browser voice app means *being* the user: open the tab, join the call, talk, listen, try to break it — by hand, every time you touch a prompt. voicebox automates that loop. It's an MCP server that turns Claude into a synthetic voice user: Claude drives a real Chromium, speaks into the page's mic via local Kokoro TTS, and hears the agent's replies back via local Whisper.
Hand it a task — *"start the story, interrupt halfway, then ask something off-topic"* — and it holds the conversation against your app and reports what happened.

Works against anything using `getUserMedia` + WebRTC (Daily, LiveKit, plain `RTCPeerConnection`), with the app none the wiser. Local models by default — no API keys to run it.

**Use it if** you're building a browser-based voice agent and you're tired of being its only tester.
**Skip it (for now) if** your app isn't web-based or runs voice entirely server-side with no browser mic.

When your coding agent starts a browser session in Voicebox, it drives a Playwright-controlled Chromium with an audio shim injected; the shim hijacks the page's microphone (fed by Kokoro TTS from this server) and tees the bot's remote WebRTC audio back to Whisper. The LLM can then act as a synthetic voice user against any web voice app — Daily, LiveKit, plain `RTCPeerConnection`, anything that uses `getUserMedia` + WebRTC — without the app being aware of the indirection.

## Topology

```
Claude (LLM) ─HTTP/JSON-RPC─► voicebox ────multiprocessing.Queue──► Pipecat child (WebsocketServerTransport)
                                    │                                       ▲
                                    │                                       │ raw 16-bit PCM
                                    │                                       │ (Kokoro 48 kHz out,
                                    │                                       │  Whisper 16 kHz in)
                                    │                                       ▼
                                    └─CDP─► Playwright-driven Chromium ◄── shim.js injected
                                                    │
                                                    │ WebRTC
                                                    ▼
                                            the target voice app
                                            (Daily, LiveKit, plain RTCPeerConnection, …)
```

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A browser-based voice app to point it at (e.g. a locally-running Next.js / Svelte app on `localhost:3000`)

By default the agent uses local models — Whisper for STT, Kokoro for TTS — so no API keys are needed.

**Whisper runs on the CPU.** On non-Apple platforms voicebox pins faster-whisper to `device="cpu"`,
`compute_type="int8"` rather than letting it auto-detect a GPU. Auto-detect selects CUDA whenever a
GPU is visible and then fails to load `libcublas.so.12` / cuDNN, which voicebox intentionally does
not depend on — keeping the install CUDA-free is worth the slower transcription for a synthetic
tester. On Apple Silicon, Whisper-MLX is used instead and runs on the GPU. If you want CUDA, install
`nvidia-cublas-cu12` + `nvidia-cudnn-cu12` yourself and change `_create_stt_service` in
`src/voicebox/agent.py`.

## Install

```bash
git clone https://github.com/IsisChameleon/voicebox.git
uv tool install -e /path/to/voicebox
```

## Run

```bash
voicebox
```

The server listens on `http://localhost:9090/mcp` (streamable-HTTP transport).

## Register with your MCP client

Claude Code:

```bash
claude mcp add voicebox --transport http http://localhost:9090/mcp --scope user
```

Cursor (`~/.cursor/mcp.json`):

```json
{ "mcpServers": { "voicebox": { "url": "http://localhost:9090/mcp" } } }
```

## MCP tools

| Tool | Purpose |
|---|---|
| `start_browser_session(url, headless?, cdp_port?, audio_port?, user_data_dir?, record_dir?)` | Launch a Playwright Chromium with the audio shim injected, navigate to `url`, expose CDP. The shim hijacks the page's mic (fed by Kokoro) and tees the page's WebRTC remote audio into Whisper. Returns `{cdp_endpoint, audio_ws_url, playwright_mcp_env, attach_hint}`. Drive the UI with any Playwright client that can attach over CDP (see below). Pass `user_data_dir` (a persistent Chrome profile) to reuse an authenticated session: log in once and stay logged in on later runs with the same dir. Pass `record_dir` to have `stop()` write the session artifacts (see below). |
| `speak(text, wait_for_playout?, wait_for_turn?, when?, timer_secs?)` | Synthesize `text` with Kokoro TTS and stream it into the shim's synthetic mic. Returns `{queued: true}` as soon as frames are queued, not when audio has finished playing. `wait_for_playout` instead returns after our own audio finishes, with `played` / `started_at` / `finished_at` / `interrupted` (or `played: false` + `reason` if unobserved within the text-scaled window). `wait_for_turn` waits for the app bot to fall silent first (the polite path) and adds `waited_for_turn_secs`. `when` / `timer_secs` arm a barge-in trigger and return `{armed: true}` (see below). |
| `listen(timeout=30, cursor=0)` | Block until at least one event exists past `cursor`, then return `{events, cursor, transcription_lag_secs}` covering everything from `cursor` onward (each batch sorted by `t`). Pass the returned `cursor` to the next call to resume without missing or re-reading anything; `cursor=0` replays the whole session. `events` is empty on timeout; a non-zero lag means a transcript is still being decoded. |
| `stop()` | Tear down the pipecat agent and close the Chromium session. Returns `{stopped: true}`, plus `artifacts` (absolute paths) when the session ran with `record_dir`. |

The IPC between the MCP server and the pipecat child is full-duplex: every command
carries a correlation id and runs as its own task, so a `speak` issued while a
`listen` is still blocked executes immediately. That is what makes talking over
the bot possible at all.

### Conversation events

`listen()` returns a monotonic, timestamped event log rather than a transcript
string. Two parties: `app_bot` is the app's voice agent under test, `tester` is
our synthetic human. Every event carries `t` (wall-clock seconds).

| Event | Meaning |
|---|---|
| `session_started` | Log header; carries `vad_stop_secs`. |
| `session_stopped` | The session is tearing down; a pending `listen()` returns this instead of being cancelled. |
| `client_connected` / `client_disconnected` | The in-page audio link came up / dropped. A drop is a status event, not speech. |
| `app_bot_speech_started` / `app_bot_speech_stopped` | The app bot's voice activity. `app_bot_speech_stopped.t` lands about `vad_stop_secs` (~1 s) after it truly stopped talking. |
| `app_bot_transcript` | A finished app-bot utterance: `text` plus `turn_started_at`. Arrives after the matching `app_bot_speech_stopped` (batch STT). |
| `tester_speech_started` / `tester_speech_stopped` / `tester_speech_interrupted` | Our synthetic voice starting / finishing / being cut off at playout. |
| `tester_transcript` | The exact text we spoke: ground-truth `speak()` input, not STT. |
| `tester_barge_in_armed` / `tester_barge_in_fired` | A `when` trigger was armed / fired. |

To wait for the next thing the app bot says, call `listen()` in a loop with the
advancing cursor and act on `app_bot_transcript`.

### Barge-in

We never interrupt the bot directly: it is a black box reached only through its
microphone. Barge-in means timing our own speech relative to the bot's and then
reading its reaction out of the event log.

```jsonc
// arm a one-shot trigger: next time the bot starts talking, wait 1.5 s, then speak
{"name": "speak", "arguments": {"text": "wait, go back",
                                "when": "app_bot_speech_started",
                                "timer_secs": 1.5}}
// → {"armed": true}   (returns immediately; the trigger fires in the audio child)
```

`when` takes any event type from the table above. The trigger is armed at call
time and reacts only to the next occurrence after arming, so the moment is picked
at audio rate with no LLM in the hot path, which makes it reproducible.
`when` and `wait_for_turn` are mutually exclusive.

### Session artifacts

Pass `record_dir` to `start_browser_session` and `stop()` writes a reviewable
report there, returning the absolute paths:

| Artifact | Contents |
|---|---|
| `merged.wav` | Stereo: tester on the left channel, app bot on the right (EVA's convention). |
| `kokoro_voice.wav` / `ember_voice.wav` | The same two sides as mono files. |
| `events.json` | The full event log, the same objects `listen()` returns. |
| `metrics.json` | Computed report: `session` (span plus `biases` for reading the numbers correctly), `turns` (per-turn transcript, app-bot turns carry `response_latency_secs`), `app_response_latencies_secs`, `talk_over_windows`, `dead_air_gaps`, `talk_time`, `utterances`, `summary`. |

The `biases` block matters: `app_bot_speech_stopped` is late by `vad_stop_secs`,
so subtract it before quoting any latency number.

### Example session

```jsonc
// 1. launch a Playwright Chromium with the audio shim injected, navigate to the app
{"name": "start_browser_session", "arguments": {"url": "http://localhost:3000"}}
// → {"cdp_endpoint": "http://localhost:9222", "audio_ws_url": "ws://localhost:9091"}

// 2. attach a Playwright client to that cdp_endpoint and drive the UI
//    (log in, navigate to the book / "Start reading" / whatever the entry point is).
//    The page then calls getUserMedia → the shim returns a synthetic mic stream
//    the MCP server feeds. Pick whichever client you already have:
//
//      @playwright/mcp:   npx @playwright/mcp@latest --cdp-endpoint=http://localhost:9222
//                         (it attaches to our browser instead of launching its own;
//                          leave its --user-data-dir unset — incompatible with --cdp-endpoint.
//                          Persist auth via this server's user_data_dir instead.)
//      your own script:   browser = await playwright.chromium.connect_over_cdp("http://localhost:9222")
//                         (see scripts/smoke_browser_shim.py for a connect_over_cdp example)

// 3. speak — the page's WebRTC peer sends OUR Kokoro audio to the bot
{"name": "speak", "arguments": {"text": "Hi Ember! Tell me about this book."}}

// 4. listen — the bot's remote audio track is teed to Whisper via the shim
{"name": "listen", "arguments": {"timeout": 45}}
// → {"events": [{"type": "app_bot_speech_started", "t": 1712.44},
//               {"type": "app_bot_transcript", "t": 1715.01,
//                "text": "Hello, welcome. I'm so excited to have you..."}],
//    "cursor": 7}
//    pass cursor: 7 to the next listen() to pick up where this left off

// 5. end the call either by saying "goodbye" (if the bot supports
//    UserVerballyInitiatedDisconnect), or click the End-call button via
//    Playwright, then:
{"name": "stop", "arguments": {}}
```

## Architecture notes

- The MCP server (parent process) hosts the FastMCP HTTP endpoint. A separate Pipecat child process runs the audio pipeline — they communicate over `multiprocessing.Queue`. A second child runs Playwright/Chromium. This keeps Pipecat's event loop and audio threads off the MCP request path.
- Audio never crosses the MCP boundary. MCP carries text and control only; audio flows out-of-band over a raw-PCM WebSocket between the shim and pipecat.
- The shim taps the **page's playout audio path** via Web Audio (`MediaStreamAudioSourceNode → AudioWorkletNode`), not the WebCodecs path. Web Audio is pulled at a fixed sample rate so silence in the source becomes literal zero samples — preserving real-time pacing for the recording and STT.
- We disable Pipecat's default RTVI processor (`enable_rtvi=False`). RTVI is meant for browser SDK clients to render UI; nothing here subscribes to it.

### File map

| File | Role |
|---|---|
| `server.py` | FastMCP HTTP surface — the tools Claude calls. Lives in the parent process. No audio code. |
| `agent_ipc.py` | The shared mailbox between parent and child. Owns the multiprocessing queues and the pipecat-child lifecycle. |
| `bot.py` | The pipecat child's tiny command loop: `read → dispatch → respond`. |
| `agent.py` | `PipecatMCPAgent` — the wrapper that owns the Pipecat pipeline behind a `WebsocketServerTransport`. |
| `runner_args.py` | The `BrowserShimRunnerArguments` dataclass (host, port, mic_rate, tap_rate, record_dir) — pipecat doesn't ship one for plain WebSocket-server transports. |
| `raw_pcm_serializer.py` | Tiny `FrameSerializer` that exchanges raw 16-bit LE mono PCM with the browser shim — no protobuf, no envelope. |
| `shim.js` | The browser shim. Injected via Playwright `addInitScript` so it runs before any page code. Overrides `getUserMedia` to return a synthetic mic stream backed by `MediaStreamTrackGenerator`, and wraps `RTCPeerConnection` to tap every inbound audio track via Web Audio (`MediaStreamAudioSourceNode → AudioWorkletNode`) back to the server. |
| `browser_session.py` | Manages the Playwright child process: launches Chromium with `--remote-debugging-port=<cdp_port>` + `--use-fake-ui-for-media-stream`, registers `shim.js` via `add_init_script`, navigates to the user-supplied URL, parks until told to stop. |

### What happens on `start_browser_session`

```
1.  Claude → MCP                   tools/call start_browser_session(url="http://localhost:3000")
2.  server.py:start_browser_session()
                                   audio_ws_url = "ws://localhost:9091"
                                   start_pipecat_process(BrowserShimRunnerArguments(port=9091, …))
                                   start_browser(url, audio_ws_url, cdp_port=9222, …)
3.  [CHILD-1: pipecat]             create_agent → WebsocketServerTransport with RawPCMSerializer
                                     audio_in_sample_rate  = 16000 (Whisper-MLX requires 16 kHz)
                                     audio_out_sample_rate = 48000 (Kokoro → page mic)
                                   pipeline: transport.input → Whisper → aggregator → Kokoro → transport.output
                                   websocket listening on :9091
4.  [CHILD-2: browser]             read shim.js, prepend window.__VOICE_SHIM_WS_URL__
                                   chromium.launch(args=[--remote-debugging-port=9222,
                                                         --use-fake-ui-for-media-stream])
                                   context.add_init_script(shim) ; page.goto(url) ; ready
5.  [page]                         shim runs before any page code:
                                     - opens WebSocket to ws://localhost:9091
                                     - overrides navigator.mediaDevices.getUserMedia
                                     - wraps window.RTCPeerConnection
                                   when the page calls getUserMedia({audio}), the shim returns
                                   MediaStream([MediaStreamTrackGenerator]). When the page creates
                                   an RTCPeerConnection, the wrapper subscribes to its `track` events.
6.  External Playwright client     connect_over_cdp("http://localhost:9222")
    (Claude's @playwright/mcp,     drives the UI: login, navigate, click "Start reading", etc.
     playwright-cli, your own)     → the app calls getUserMedia (shim returns synthetic mic)
                                   → the app creates RTCPeerConnection to its SFU
                                   → bot starts streaming audio → shim's track-event hook
                                     pipes it via Web Audio worklet → WebSocket → pipecat
7.  [Claude] speak("hi ember")     Kokoro renders audio → WebsocketServerTransport writes Int16 PCM
                                   over the WS → shim writes AudioData chunks into the
                                   MediaStreamTrackGenerator → the page's WebRTC peer encodes Opus
8.  [Claude] listen()              VAD/SmartTurn waits for the bot's utterance to end →
                                   Whisper transcript appended to the event log →
                                   events past the cursor returned to MCP
9.  [Claude] stop()                flushes artifacts (if record_dir), then terminates
                                   pipecat child + browser child
```

### Tradeoffs and known sharp edges

- **Whisper-MLX requires 16 kHz on input.** `mlx_whisper.transcribe()` has no `sample_rate` parameter and hard-assumes 16 kHz. The shim's outbound `AudioContext` runs at 16 kHz so the browser does the 48→16 resample natively; Kokoro stays at 48 kHz so the synthetic mic into the page is full-quality.
- **Sample-rate split is asymmetric on the wire:** `audio_in_sample_rate=16000` (browser → pipecat), `audio_out_sample_rate=48000` (pipecat → browser). The `AudioBufferProcessor` resamples internally so the recorded WAVs come out at 48 kHz regardless.
- **VAD `stop_secs=1.0s`** captures complete utterances over WebRTC with natural pauses; pipecat's default 0.2 s (tuned for clean TTS sources) chops remote speech mid-sentence.
- **The shim taps audio via Web Audio, not WebCodecs**, because `MediaStreamTrackProcessor` only emits chunks during active speech on a remote WebRTC track — silence is dropped, so a sparse byte stream reaches pipecat and the recorded WAV plays back several times faster than real time. Web Audio is pulled by the AudioContext clock and fills silence with zero samples.
- **Headless Chromium works, audio path included** — `headless=true` still captures the bot's audio and feeds the synthetic mic (the tap is Web Audio, not a visible window). The shim relies on Web Audio + `MediaStreamTrackGenerator` (modern Chromium-only). Tested with Playwright 1.50 + bundled Chromium.
- **`RTCPeerConnection` wrap won't catch peer connections inside cross-origin iframes or Web Workers.** Not an issue for the readme app, but a real limitation for apps using Daily Prebuilt's `<DailyIframe>` (workaround: hook `<audio>` elements via `MutationObserver` + `captureStream()`).
- **One session at a time.** The server pins ports 9090 (MCP), 9091 (audio WS), 9222 (CDP). `start_browser_session` checks `audio_port`/`cdp_port` are free first and fails with a clear message if not — pass overrides to run a second session in parallel.
- **Session reuse uses `user_data_dir`.** A persistent profile lives in the browser's *default* context, which is exactly what a CDP-attached client sees — so logging in once persists across runs with no save step. (Playwright's `storage_state` JSON is deliberately *not* supported: it loads into a separate non-default context that a CDP-attached client can't save back, so it can't be generated from within a session — a persistent profile does the job without that footgun.)
- We pre-grant `microphone` permission via `--use-fake-ui-for-media-stream`. No permission prompt to dismiss.

## License

BSD-2-Clause (inherited from upstream).
