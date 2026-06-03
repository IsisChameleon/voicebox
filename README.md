# voicebox

MCP server that gives an LLM agent **voice + ears in a browser**. The agent drives a Playwright-controlled Chromium with an audio shim injected; the shim hijacks the page's microphone (fed by Kokoro TTS from this server) and tees the bot's remote WebRTC audio back to Whisper. The LLM can then act as a synthetic voice user against any web voice app — Daily, LiveKit, plain `RTCPeerConnection`, anything that uses `getUserMedia` + WebRTC — without the app being aware of the indirection.

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
| `start_browser_session(url, headless?, cdp_port?, audio_port?, user_data_dir?, storage_state?)` | Launch a Playwright Chromium with the audio shim injected, navigate to `url`, expose CDP. The shim hijacks the page's mic (fed by Kokoro) and tees the page's WebRTC remote audio into Whisper. Returns `{cdp_endpoint, audio_ws_url}`. Drive the UI with any Playwright client that can attach over CDP (see below). To reuse an authenticated session, pass `user_data_dir` (persistent profile — log in once, stays logged in; recommended) or `storage_state` (a cookies/localStorage JSON produced out-of-band). |
| `speak(text)` | Synthesize `text` with Kokoro TTS and stream it into the shim's synthetic mic. Returns when frames are queued — not when audio has finished playing. |
| `listen(timeout=30)` | Block until the other side completes an utterance (VAD-segmented). Returns the transcribed text, or `""` on timeout. A long reply produces multiple utterances; call `listen()` in a loop. |
| `stop()` | Tear down the pipecat agent and close the Chromium session. |

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
//                          Persist auth via this server's user_data_dir/storage_state instead.)
//      your own script:   browser = await playwright.chromium.connect_over_cdp("http://localhost:9222")
//                         (see scripts/e2e_readme_call.py for a full login→navigate→call driver)

// 3. speak — the page's WebRTC peer sends OUR Kokoro audio to the bot
{"name": "speak", "arguments": {"text": "Hi Ember! Tell me about this book."}}

// 4. listen — the bot's remote audio track is teed to Whisper via the shim
{"name": "listen", "arguments": {"timeout": 45}}
// → "Hello, welcome. I'm so excited to have you..."

// 5. end the call either by saying "goodbye" (if the bot supports
//    UserVerballyInitiatedDisconnect), or click the End-call button via
//    Playwright, then:
{"name": "stop", "arguments": {}}
```

See `scripts/e2e_readme_call.py` for a complete driver that does
login → navigate → call → conversation → end against the readme app.

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
                                   Whisper transcript returned to MCP
9.  [Claude] stop()                terminates pipecat child + browser child
```

### Tradeoffs and known sharp edges

- **Whisper-MLX requires 16 kHz on input.** `mlx_whisper.transcribe()` has no `sample_rate` parameter and hard-assumes 16 kHz. The shim's outbound `AudioContext` runs at 16 kHz so the browser does the 48→16 resample natively; Kokoro stays at 48 kHz so the synthetic mic into the page is full-quality.
- **Sample-rate split is asymmetric on the wire:** `audio_in_sample_rate=16000` (browser → pipecat), `audio_out_sample_rate=48000` (pipecat → browser). The `AudioBufferProcessor` resamples internally so the recorded WAVs come out at 48 kHz regardless.
- **VAD `stop_secs=1.0s`** captures complete utterances over WebRTC with natural pauses; pipecat's default 0.2 s (tuned for clean TTS sources) chops remote speech mid-sentence.
- **The shim taps audio via Web Audio, not WebCodecs**, because `MediaStreamTrackProcessor` only emits chunks during active speech on a remote WebRTC track — silence is dropped, so a sparse byte stream reaches pipecat and the recorded WAV plays back several times faster than real time. Web Audio is pulled by the AudioContext clock and fills silence with zero samples.
- **Headless Chromium works, audio path included** — `headless=true` still captures the bot's audio and feeds the synthetic mic (the tap is Web Audio, not a visible window). The shim relies on Web Audio + `MediaStreamTrackGenerator` (modern Chromium-only). Tested with Playwright 1.50 + bundled Chromium.
- **`RTCPeerConnection` wrap won't catch peer connections inside cross-origin iframes or Web Workers.** Not an issue for the readme app, but a real limitation for apps using Daily Prebuilt's `<DailyIframe>` (workaround: hook `<audio>` elements via `MutationObserver` + `captureStream()`).
- **One session at a time.** The server pins ports 9090 (MCP), 9091 (audio WS), 9222 (CDP). `start_browser_session` checks `audio_port`/`cdp_port` are free first and fails with a clear message if not — pass overrides to run a second session in parallel.
- **Session reuse: prefer `user_data_dir` over `storage_state`.** A persistent profile lives in the browser's *default* context, which is exactly what a CDP-attached client sees — so logging in once persists across runs with no save step. `storage_state` is loaded into a separate (non-default) context: the shim page does use those cookies (auth is restored), but a CDP-attached client can neither list them via `context.cookies()` nor save them via `context.storage_state()` — Playwright's CDP view targets the default context. So a `storage_state` file must be produced out-of-band by a standalone (non-CDP) Playwright login script.
- We pre-grant `microphone` permission via `--use-fake-ui-for-media-stream`. No permission prompt to dismiss.

## License

BSD-2-Clause (inherited from upstream).
